"""
Kaggriculture agent — generalized multi-crop task-queue / scheduler agent
with drip-sell market ordering and a daily strategist for hiring / land.

Architecture (see AGENTS.md / README.md for full game rules):

  1. State parsing      - read obs into plain locals
  2. Daily strategist    - runs off obs["hires_today"] / unlocked_quadrants
                           every turn (cheap enough not to gate on hour==0):
                           decide how many hands to hire today and whether
                           to buy the next land quadrant
  3. Task queue build    - scan every unlocked tile, emit a prioritized task
                           per tile; plot cap now scales with unit count
  4. Unit scheduling     - greedily assign each idle unit (farmer + hands)
                           to its nearest highest-priority reachable task
  5. Action execution    - move one step toward the assigned tile, or act
                           if already there
  6. Market orders       - land purchase attempt, hire attempts, drip-sell
                           shed contents, keep seed stock topped up

Build order (from the architecture writeup):
  [DONE] 1. generalized crop loop, no strategist, fixed rotation, plot cap
  [DONE] 2. market order builder with drip-selling
  [THIS STEP] 3. daily strategist for hiring + land expansion
  [NEXT]      4. fertilizer timing, animals, smarter market reads

Deliberately still NOT included:
  - animals / coops / pastures / fertilizer
  - crop-mix rebalancing based on live market prices (rotation is still
    fixed -- the strategist only decides hiring/land, not what to plant)
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
# turn to weeds, which is worse than not planting them at all. 8 tiles/day
# is what one unit can reliably water+harvest -- this now scales with the
# number of units actually on the farm (see PLOTS_PER_UNIT below) instead
# of being a fixed constant, so hiring hands raises the ceiling automatically.
PLOTS_PER_UNIT = 8

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


def _build_tasks(obs, me, plot_cap):
    """Scan the farm and return a priority-sorted list of tile tasks."""
    step = obs.get("step", obs["day"] * TURNS_PER_DAY + obs["hour"])
    tiles = me["tiles"]
    board_size = len(tiles)
    half = board_size // 2
    unlocked = set(me["unlocked_quadrants"])

    # Count tiles already under cultivation so we don't take on more land
    # than the current unit count can water/harvest in a day.
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
                if active_plots >= plot_cap:
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
# Daily strategist — Step 3: hiring + land expansion
# ---------------------------------------------------------------------------
# Both decisions are cheap to recompute every turn (no state machine needed):
# hiring reads straight off obs["hires_today"] (resets to 0 each day per the
# rules), and land purchase reads straight off unlocked_quadrants. Re-running
# the same check every turn just means "keep trying until it succeeds or the
# budget runs out for today" -- simpler than gating on hour == 0 and it
# self-corrects if an order silently no-ops for any reason.

FARM_HAND_COST_MULT = 1  # matches configuration default; adjust if the env config differs

# Fib cost climbs 1,1,2,3,5,8,13,21,... -- stop hiring once the *next* hire
# would cost more than this. 13 lets us take up to 7 hands/day (cumulative
# cost 1+1+2+3+5+8+13=33) before the 8th hire (21) prices itself out.
HIRE_COST_CEILING = 13
MAX_HIRES_PER_DAY = 7

# Cash we refuse to dip below when hiring or buying land -- keeps seed
# top-ups and drip-selling reserves from starving out entirely on a
# hire/land spree.
CASH_RESERVE = 200

LAND_ORDER = ["NE", "SW", "SE"]
LAND_COST = {"NE": 1000, "SW": 2000, "SE": 4000}
# Extra buffer on top of CASH_RESERVE specifically for land, since it's a
# big one-time spend -- don't buy a quadrant if it would leave us unable to
# hire or restock seeds for the rest of the day.
LAND_BUFFER = 500


def _fib(n):
    """0-indexed: fib(0)=1, fib(1)=1, fib(2)=2, fib(3)=3, fib(4)=5, ..."""
    a, b = 1, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def _plan_hires(money, hires_today):
    """Return how many additional HIRE orders to attempt this turn."""
    n = hires_today
    budget = money - CASH_RESERVE
    planned = 0
    while planned < MAX_HIRES_PER_DAY:
        cost = FARM_HAND_COST_MULT * _fib(n)
        if cost > HIRE_COST_CEILING or cost > budget:
            break
        budget -= cost
        n += 1
        planned += 1
    return planned


def _plan_land_purchase(unlocked, money):
    """Return the next quadrant to buy this turn, or None."""
    for quadrant in LAND_ORDER:
        if quadrant in unlocked:
            continue
        cost = LAND_COST[quadrant]
        if money - cost >= CASH_RESERVE + LAND_BUFFER:
            return quadrant
        return None  # next-in-order quadrant unaffordable -> nothing to buy yet
    return None  # fully expanded


# ---------------------------------------------------------------------------
# Market order builder — Step 2: drip-selling (unchanged from last pass)
# ---------------------------------------------------------------------------
PREMIUM_GOODS = {"STRAWBERRY", "MELON", "MILK", "WOOL"}
DRIP_CAP_PREMIUM = 2   # max units/turn for strawberry/melon/milk/wool
DRIP_CAP_STAPLE = 8    # max units/turn for wheat/carrot/tomato/eggs/fertilizer

_sell_history = {}
SELL_HISTORY_WINDOW = TURNS_PER_DAY  # look back one in-game day


def _recent_sold(item, step):
    hist = [(s, q) for s, q in _sell_history.get(item, []) if step - s <= SELL_HISTORY_WINDOW]
    _sell_history[item] = hist
    return sum(q for _, q in hist)


def _record_sale(item, qty, step):
    _sell_history.setdefault(item, []).append((step, qty))


def _build_market_orders(obs, me, private, market, step):
    shed = private.get("shed", {})
    seeds = private.get("seeds", {})
    money = me["money"]
    prices = market.get("prices", {})
    unlocked = set(me["unlocked_quadrants"])
    hires_today = me.get("hires_today", 0)

    orders = []

    # --- Land purchase (big one-time spend, try first while cash is high) --
    quadrant = _plan_land_purchase(unlocked, money)
    if quadrant is not None:
        orders.append(["BUY_LAND"])
        money -= LAND_COST[quadrant]

    # --- Hire hands for today -----------------------------------------------
    # Re-derive the plan against money *after* any land purchase above, so we
    # don't double-spend the same cash on both in one turn.
    n_hires = _plan_hires(money, hires_today)
    for _ in range(n_hires):
        if len(orders) >= 10:
            break
        orders.append(["HIRE"])
        money -= FARM_HAND_COST_MULT * _fib(hires_today)
        hires_today += 1

    # --- Drip-sell shed contents ---------------------------------------------
    # Sell highest-price-per-unit items first: with only maxMarketOrdersPerTurn
    # (10) slots per turn, we don't want a big pile of cheap wheat crowding
    # out a strawberry/melon sale that's actually worth more per order.
    sellable = [(item, count) for item, count in shed.items() if count > 0]
    sellable.sort(key=lambda kv: -prices.get(kv[0], 1))

    for item, count in sellable:
        if len(orders) >= 10:
            break

        per_turn_cap = DRIP_CAP_PREMIUM if item in PREMIUM_GOODS else DRIP_CAP_STAPLE
        daily_ceiling = per_turn_cap * 4  # soft rolling-day ceiling, see _recent_sold
        room = max(0, daily_ceiling - _recent_sold(item, step))

        qty = min(count, per_turn_cap, room)
        if qty <= 0:
            continue

        orders.append(["SELL", item, qty])
        _record_sale(item, qty, step)

    # --- Keep seed stock topped up -------------------------------------------
    for crop in CROP_CONFIG:
        if len(orders) >= 10:
            break
        cost = CROP_CONFIG[crop]["seed_cost"]
        have = seeds.get(crop, 0)
        if have < 3 and money >= cost + CASH_RESERVE:
            buy_n = min(5, int((money - CASH_RESERVE) // cost))
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

    plot_cap = PLOTS_PER_UNIT * len(units)

    tasks = _build_tasks(obs, me, plot_cap)
    assignment = _assign_tasks(units, tasks)
    unit_actions = _build_unit_actions(units, assignment)

    farmer_action = unit_actions.get("farmer", ["PASS"])
    hand_actions = [unit_actions[f"hand{i}"] for i in range(len(me.get("hands", [])))]

    market_orders = _build_market_orders(obs, me, private, market, step)

    return {"farmer": farmer_action, "hands": hand_actions, "market": market_orders}


if __name__ == "__main__":
    # Quick local smoke test: python3 main.py
    from kaggle_environments import make

    env = make("kaggriculture", configuration={"episodeSteps": 720}, debug=True)
    env.run([agent, "starter"])
    final = env.steps[-1]
    for i, s in enumerate(final):
        print(f"Player {i}: reward={s.reward}, status={s.status}")