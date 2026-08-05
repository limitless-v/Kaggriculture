"""
Kaggriculture agent — generalized multi-crop task-queue / scheduler agent
with drip-sell market ordering.

Architecture (see AGENTS.md / README.md for full game rules):

  1. State parsing     - read obs into plain locals, no strategist/expansion yet
  2. Task queue build   - scan every unlocked tile, emit a prioritized task per tile
  3. Unit scheduling    - greedily assign each idle unit (farmer + hands) to its
                          nearest highest-priority reachable task
  4. Action execution   - move one step toward the assigned tile, or act if there
  5. Market orders       - DRIP-sell shed contents (capped per item per turn,
                          with a rolling per-item daily ceiling), keep seed
                          stock topped up

Build order (from the architecture writeup):
  [DONE] 1. generalized crop loop, no strategist, fixed rotation, 8-plot cap
  [THIS STEP] 2. market order builder with drip-selling
  [NEXT]      3. daily strategist for land/hiring
  [LATER]     4. fertilizer timing, learned/tuned heuristics

Deliberately still NOT included:
  - land purchases (BUY_LAND), hiring (HIRE) -- code supports hands if
    present, just doesn't hire any yet
  - animals / coops / pastures / fertilizer
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


# ---------------------------------------------------------------------------
# Market order builder — Step 2: drip-selling
# ---------------------------------------------------------------------------
# Goods with steep "sq" / "sqrt"-style downside curves (see README Price
# Function table) crash hard on oversupply -- strawberry/melon/milk/wool hit
# the $1 floor at just I0 + a handful of units sold. Dumping a full shed of
# these in one SELL order is the single easiest way to torch your own
# margin. Staples (wheat/carrot/tomato/eggs) absorb oversupply more gently
# and can be sold in bigger chunks without much price damage.
PREMIUM_GOODS = {"STRAWBERRY", "MELON", "MILK", "WOOL"}

DRIP_CAP_PREMIUM = 2   # max units/turn for strawberry/melon/milk/wool
DRIP_CAP_STAPLE = 8    # max units/turn for wheat/carrot/tomato/eggs/fertilizer

# We only see *our own* sell orders here (opponent's are private to their
# agent), but that's exactly the leverage this fixes: a single turn's price
# doesn't tell you whether you already leaned on this item hard three turns
# ago. Track our own recent volume per item so repeated small drips don't
# silently add up to a shed-dump spread across a few turns.
# Module-level so it persists across agent() calls within one episode (the
# process isn't restarted between turns).
_sell_history = {}
SELL_HISTORY_WINDOW = TURNS_PER_DAY  # look back one in-game day


def _recent_sold(item, step):
    hist = [(s, q) for s, q in _sell_history.get(item, []) if step - s <= SELL_HISTORY_WINDOW]
    _sell_history[item] = hist
    return sum(q for _, q in hist)


def _record_sale(item, qty, step):
    _sell_history.setdefault(item, []).append((step, qty))


def _build_market_orders(me, private, market, step):
    shed = private.get("shed", {})
    seeds = private.get("seeds", {})
    money = me["money"]
    prices = market.get("prices", {})

    orders = []

    # --- Drip-sell shed contents ------------------------------------------
    # Sell highest-price-per-unit items first: with only maxMarketOrdersPerTurn
    # (10) slots per turn, we don't want a big pile of cheap wheat crowding
    # out a strawberry/melon sale that's actually worth more per order.
    sellable = [(item, count) for item, count in shed.items() if count > 0]
    sellable.sort(key=lambda kv: -prices.get(kv[0], 1))

    for item, count in sellable:
        if len(orders) >= 10:
            break

        per_turn_cap = DRIP_CAP_PREMIUM if item in PREMIUM_GOODS else DRIP_CAP_STAPLE

        # Soft rolling-day ceiling on top of the per-turn cap: even spread
        # across many turns, pushing more than ~4x the per-turn cap into the
        # market inside one day is still enough to crash a premium good.
        daily_ceiling = per_turn_cap * 4
        room = max(0, daily_ceiling - _recent_sold(item, step))

        qty = min(count, per_turn_cap, room)
        if qty <= 0:
            continue

        orders.append(["SELL", item, qty])
        _record_sale(item, qty, step)

    # --- Keep seed stock topped up -----------------------------------------
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
    market = obs["market"]
    step = obs.get("step", obs["day"] * TURNS_PER_DAY + obs["hour"])

    units = [("farmer", tuple(me["farmer"]))]
    for i, hpos in enumerate(me.get("hands", [])):
        units.append((f"hand{i}", tuple(hpos)))

    tasks = _build_tasks(obs, me)
    assignment = _assign_tasks(units, tasks)
    unit_actions = _build_unit_actions(units, assignment)

    farmer_action = unit_actions.get("farmer", ["PASS"])
    hand_actions = [unit_actions[f"hand{i}"] for i in range(len(me.get("hands", [])))]

    market_orders = _build_market_orders(me, private, market, step)

    return {"farmer": farmer_action, "hands": hand_actions, "market": market_orders}


if __name__ == "__main__":
    # Quick local smoke test: python3 main.py
    from kaggle_environments import make

    env = make("kaggriculture", configuration={"episodeSteps": 720}, debug=True)
    env.run([agent, "starter"])
    final = env.steps[-1]
    for i, s in enumerate(final):
        print(f"Player {i}: reward={s.reward}, status={s.status}")