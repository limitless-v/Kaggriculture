"""
Kaggriculture agent — generalized multi-crop task-queue / scheduler agent.

Architecture (see AGENTS.md / README.md for full game rules):

  1. State parsing    - read obs into plain locals, no strategist/expansion logic yet
  2. Task queue build  - scan every unlocked tile, emit a prioritized task per tile
  3. Unit scheduling   - greedily assign each idle unit (farmer + any hands) to its
                         nearest highest-priority reachable task
  4. Action execution  - move one step toward the assigned tile, or act if already there
  5. Market orders     - sell everything sitting in the shed, keep seed stock topped up

Deliberately NOT included in this version (fixed crop mix, no expansion):
  - land purchases (BUY_LAND)
  - hiring farm hands (HIRE)       -- code supports hands if present, just doesn't hire any
  - animals / coops / pastures
  - fertilizer

All of those are natural next steps once this loop is beating the baselines.
"""

# ---------------------------------------------------------------------------
# Static knowledge: crop economics (README.md "Object Types" table)
# ---------------------------------------------------------------------------

CROP_CONFIG = {
    "WHEAT":      {"seed_cost": 10,  "ongoing": False},
    "CARROT":     {"seed_cost": 20,  "ongoing": False},
    "MELON":      {"seed_cost": 80,  "ongoing": False},
    "TOMATO":     {"seed_cost": 50,  "ongoing": True},
    "STRAWBERRY": {"seed_cost": 100, "ongoing": True},
}

# Fixed planting rotation for empty tiles -- cheap staples get more tiles than
# expensive ongoing crops, since ongoing crops occupy a tile far longer per
# seed dollar spent (see the Yield/tile/day figures in README.md).
CROP_ROTATION = [
    "WHEAT", "WHEAT", "CARROT", "WHEAT", "CARROT",
    "MELON", "TOMATO", "CARROT", "WHEAT", "STRAWBERRY",
]

# Priorities: higher runs first. Weeds and about-to-decay harvests are
# time-critical; routine watering matters more than expanding into new tiles.
PRIORITY_URGENT_HARVEST = 95   # plant about to start losing yield to decay
PRIORITY_WEED = 60
PRIORITY_HARVEST_ONGOING = 70  # tomato/strawberry, opportunistic
PRIORITY_WATER_BASE = 50       # + 20 per consecutive_unwatered day
PRIORITY_PLANT = 20

# A lone farmer cannot keep 25 tiles watered inside a 24-turn day (that's
# ~50 turns of move+water alone) -- tiles left unwatered two days running
# turn to weeds, which is worse than not planting them at all. Cap how many
# tiles are actively farmed at once so daily maintenance fits the turn
# budget; raise this once hands are hired in a future strategist version.
MAX_ACTIVE_PLOTS = 8

# Decay for one-time crops begins exactly at max_lifespan_step, but yield
# actually stops growing a full day earlier (at max_yield_day) -- so there's
# a whole day where the crop is at peak value and just waiting to be
# collected. Flag it urgent as soon as that peak day starts, not on the
# last turn before decay -- one turn is not enough lead time for the
# farmer to travel to the tile and act.
TURNS_PER_DAY = 24
HARVEST_LEAD_TURNS = TURNS_PER_DAY


def _quadrant_of(x, y, half):
    if x < half and y < half:
        return "NW"
    if x >= half and y < half:
        return "NE"
    if x < half and y >= half:
        return "SW"
    return "SE"


def _manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _build_tasks(obs, me):
    """Scan the farm and return a priority-sorted list of tile tasks."""
    step = obs.get("step", obs["day"] * 24 + obs["hour"])
    tiles = me["tiles"]
    board_size = len(tiles)
    half = board_size // 2
    unlocked = set(me["unlocked_quadrants"])

    # Count tiles already under cultivation so we don't take on more land
    # than one farmer can water/harvest in a day (see MAX_ACTIVE_PLOTS).
    active_plots = sum(
        1
        for y in range(board_size)
        for x in range(board_size)
        if isinstance(tiles[y][x], dict) and tiles[y][x].get("kind") == "PLANT"
    )

    tasks = []
    for y in range(board_size):
        row = tiles[y]
        for x in range(board_size):
            if _quadrant_of(x, y, half) not in unlocked:
                continue
            tile = row[x]

            if tile is None:
                if active_plots >= MAX_ACTIVE_PLOTS:
                    continue
                crop = CROP_ROTATION[(x * board_size + y) % len(CROP_ROTATION)]
                tasks.append({
                    "pos": (x, y),
                    "action": ["PLANT", crop],
                    "priority": PRIORITY_PLANT,
                })
                active_plots += 1  # reserve the slot so we don't over-queue
                continue

            if not isinstance(tile, dict):
                continue

            kind = tile.get("kind")

            if kind == "WEED":
                tasks.append({"pos": (x, y), "action": ["DIG"], "priority": PRIORITY_WEED})
                continue

            if kind != "PLANT":
                # COOP / PASTURE -- no animal strategy in this version yet.
                continue

            crop = tile["crop"]
            cfg = CROP_CONFIG.get(crop, {"ongoing": False})
            yield_units = tile.get("yield_units", 0)
            watered = tile.get("watered_today", False)
            lifespan_step = tile.get("max_lifespan_step", -1)
            decaying_soon = (
                lifespan_step != -1 and step >= lifespan_step - HARVEST_LEAD_TURNS
            )

            if yield_units > 0 and decaying_soon:
                # Salvage yield before it starts eroding -- always take priority.
                tasks.append({"pos": (x, y), "action": ["HARVEST"],
                               "priority": PRIORITY_URGENT_HARVEST})
            elif yield_units > 0 and cfg["ongoing"]:
                # Ongoing crops (tomato/strawberry): harvest as soon as a
                # scheduled production lands, no benefit to waiting.
                tasks.append({"pos": (x, y), "action": ["HARVEST"],
                               "priority": PRIORITY_HARVEST_ONGOING})
            elif not watered:
                cu = tile.get("consecutive_unwatered", 0)
                tasks.append({"pos": (x, y), "action": ["WATER"],
                               "priority": PRIORITY_WATER_BASE + cu * 20})
            # else: one-time crop still growing toward its peak, already
            # watered today -- nothing useful to do here this turn.

    tasks.sort(key=lambda t: -t["priority"])
    return tasks


def _assign_tasks(units, tasks):
    """Greedy nearest-unit-to-highest-priority-task assignment."""
    remaining = list(units)
    assignment = {}
    for task in tasks:
        if not remaining:
            break
        best = min(remaining, key=lambda u: _manhattan(u[1], task["pos"]))
        assignment[best[0]] = task
        remaining.remove(best)
    return assignment


def _step_toward(pos, target):
    x, y = pos
    tx, ty = target
    if x == tx and y == ty:
        return None
    if abs(tx - x) >= abs(ty - y):
        return "EAST" if tx > x else "WEST"
    return "SOUTH" if ty > y else "NORTH"


def _build_unit_actions(units, assignment):
    actions = {}
    for name, pos in units:
        task = assignment.get(name)
        if task is None:
            actions[name] = ["PASS"]
            continue
        if tuple(pos) == task["pos"]:
            actions[name] = task["action"]
        else:
            move = _step_toward(tuple(pos), task["pos"])
            actions[name] = [move] if move else ["PASS"]
    return actions


def _build_market_orders(me, private):
    shed = private.get("shed", {})
    seeds = private.get("seeds", {})
    money = me["money"]

    orders = []

    # Sell everything harvested so far -- turns idle inventory into cash the
    # daily strategist (future work) can act on, and keeps the shed under cap.
    for item, count in shed.items():
        if count > 0:
            orders.append(["SELL", item, count])

    # Keep a small seed buffer per crop in the rotation so PLANT never stalls.
    for crop in CROP_CONFIG:
        if len(orders) >= 10:
            break
        cost = CROP_CONFIG[crop]["seed_cost"]
        have = seeds.get(crop, 0)
        if have < 3 and money >= cost:
            buy_n = min(5, int(money // cost))
            if buy_n > 0:
                orders.append(["BUY_SEED", crop, buy_n])
                money -= buy_n * cost

    return orders[:10]


def agent(obs):
    player = obs["player"]
    me = obs["farms"][player]
    private = obs["private"]

    units = [("farmer", tuple(me["farmer"]))]
    for i, hpos in enumerate(me.get("hands", [])):
        units.append((f"hand{i}", tuple(hpos)))

    tasks = _build_tasks(obs, me)
    assignment = _assign_tasks(units, tasks)
    unit_actions = _build_unit_actions(units, assignment)

    farmer_action = unit_actions.get("farmer", ["PASS"])
    hand_actions = [unit_actions[f"hand{i}"] for i in range(len(me.get("hands", [])))]

    market = _build_market_orders(me, private)

    return {"farmer": farmer_action, "hands": hand_actions, "market": market}

 #test
if __name__ == "__main__":
    # Quick local smoke test: python3 main.py
    from kaggle_environments import make

    env = make("kaggriculture", configuration={"episodeSteps": 720}, debug=True)
    env.run([agent, "random"])
    final = env.steps[-1]
    for i, s in enumerate(final):
        print(f"Player {i}: reward={s.reward}, status={s.status}")