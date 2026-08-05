"""
Kaggriculture agent — task-queue / scheduler agent with drip-selling
(now price-formula-driven), daily strategist (hire/land), animals, and
fertilizer. All economic constants below are sourced from README.md's
Object Types and Price Function tables.

Architecture:
  1. State parsing       - read obs into plain locals
  2. Daily strategist     - hiring (Fibonacci cost curve) + land expansion
  3. Task queue build     - crops, weeds, animal structures/lifecycle,
                            fertilizer timing (per-crop bonus windows)
  4. Unit scheduling      - greedy nearest-unit-to-task; tasks needing a
                            carried item (wheat/fertilizer/animal) redirect
                            to a shed PICKUP first if nobody's carrying it
  5. Action execution     - move one step toward the assigned tile/shed
  6. Market orders        - land, hire, buy animals, price-aware drip-sell,
                            seed + wheat-feed-buffer top-up

Build order:
  [DONE] 1. generalized crop loop
  [DONE] 2. drip-selling market builder
  [DONE] 3. daily strategist (hire/land)
  [DONE] 4. animals + fertilizer
  [THIS STEP] 5. real table values + price-formula-driven selling
  [NEXT]      6. crop-mix rebalancing against live prices, ROI-aware
               animal targets (goose/cow/sheep payback differs a lot --
               see Object Types comments below), opponent-aware town-demand
               timing
"""

import math

# ---------------------------------------------------------------------------
# Object Types (README.md table) -- crop economics
# ---------------------------------------------------------------------------
# bonus_window = (start_age, end_age) for one-time crops: watering in this
# window adds +1 yield/day (or +2 if fertilized). Age = day - planted_day.
# Wheat/Carrot windows follow the general ceil(max_yield_day/2) rule; Melon
# is an explicit override per the README note ("bonus window is ages 6-12",
# NOT derivable from its Time-to-Max-Yield of 10).
CROP_CONFIG = {
    "WHEAT":      {"seed_cost": 10,  "base_price": 25,  "ongoing": False,
                    "first_yield_day": 2, "max_yield_day": 4, "bonus_window": (2, 4), "max_yield": 6},
    "CARROT":     {"seed_cost": 20,  "base_price": 35,  "ongoing": False,
                    "first_yield_day": 2, "max_yield_day": 3, "bonus_window": (2, 3), "max_yield": 4},
    "MELON":      {"seed_cost": 80,  "base_price": 250, "ongoing": False,
                    "first_yield_day": 10, "max_yield_day": 10, "bonus_window": (6, 12), "max_yield": 6},
    "TOMATO":     {"seed_cost": 50,  "base_price": 60,  "ongoing": True,
                    "first_yield_day": 8, "max_yield_day": 11, "interval_days": 1, "max_yield": 4},
    "STRAWBERRY": {"seed_cost": 100, "base_price": 120, "ongoing": True,
                    "first_yield_day": 10, "max_yield_day": 16, "interval_days": 2, "max_yield": 4},
}

CROP_ROTATION = [
    "WHEAT", "WHEAT", "CARROT", "WHEAT", "CARROT",
    "MELON", "TOMATO", "CARROT", "WHEAT", "STRAWBERRY",
]

PRIORITY_URGENT_HARVEST = 95
PRIORITY_WEED = 60
PRIORITY_HARVEST_ONGOING = 70
PRIORITY_ANIMAL_HARVEST = 65
PRIORITY_FEED = 80
PRIORITY_WATER_BASE = 50
PRIORITY_FERTILIZE_ONGOING = 45
PRIORITY_FERTILIZE_ONETIME = 35
PRIORITY_CARE = 30
PRIORITY_BUILD_STRUCTURE = 30
PRIORITY_PLACE_ANIMAL = 55
PRIORITY_PLANT = 20
PRIORITY_COLLECT_FERTILIZER = 15

PLOTS_PER_UNIT = 8
TURNS_PER_DAY = 24
HARVEST_LEAD_TURNS = TURNS_PER_DAY

SHED_TILES = [(4, 4), (5, 4), (4, 5), (5, 5)]

# ---------------------------------------------------------------------------
# Animals (README.md Object Types table)
# ---------------------------------------------------------------------------
# ROI note for the future rebalancing step: at base prices, steady-state
# revenue/day is price/interval -- Goose $50/1d=$50/day, Cow $160/2d=$80/day,
# Sheep $200/3d=~$67/day -- but cow/sheep cost 400/500 vs goose's 300 and
# take longer to reach first yield (8/6 days vs goose's 4), so goose pays
# back fastest even though its steady-state rate is lower. Worth revisiting
# once ANIMAL_TARGETS becomes a ranked/ROI-driven choice instead of "one of each".
ANIMAL_CONFIG = {
    "GOOSE": {"structure": "COOP",    "product": "EGG",  "cost": 300, "base_price": 50,
               "first_yield_day": 4, "interval_days": 1, "max_held": 4},
    "COW":   {"structure": "PASTURE", "product": "MILK", "cost": 400, "base_price": 160,
               "first_yield_day": 8, "interval_days": 2, "max_held": 6},
    "SHEEP": {"structure": "PASTURE", "product": "WOOL", "cost": 500, "base_price": 200,
               "first_yield_day": 6, "interval_days": 3, "max_held": 6},
}
ANIMAL_TARGETS = {"GOOSE": 1, "COW": 1, "SHEEP": 1}
STRUCTURE_TARGETS = {"COOP": 1, "PASTURE": 2}

WHEAT_FEED_BUFFER = 10
FERTILIZER_FETCH_QTY = 1

# ---------------------------------------------------------------------------
# Price Function (README.md Market Mechanics table)
# ---------------------------------------------------------------------------
# price(inv) = base + sign * amp * f(|inv - I0|)
#   sign = +1 if inv < I0 (scarcity), -1 if inv > I0 (glut)
#   amp  = target * base / f(T)
# Floored at $1, rounded to nearest dollar. Exact replica of the game's
# formula -- lets us predict price impact *before* selling instead of
# guessing at a flat per-item cap.
MARKET_PARAMS = {
    "WHEAT":      {"base": 25,  "I0": 10000, "T": 400, "below_func": "sqrt",   "below_target": 0.80,
                    "above_func": "log",    "above_target": 0.20},
    "CARROT":     {"base": 35,  "I0": 10000, "T": 450, "below_func": "log",    "below_target": 0.20,
                    "above_func": "sqrt",   "above_target": 0.70},
    "TOMATO":     {"base": 60,  "I0": 10000, "T": 200, "below_func": "linear", "below_target": 0.40,
                    "above_func": "sqrt",   "above_target": 0.60},
    "STRAWBERRY": {"base": 120, "I0": 10000, "T": 100, "below_func": "sqrt",   "below_target": 0.70,
                    "above_func": "linear", "above_target": 1.60},
    "MELON":      {"base": 250, "I0": 10000, "T": 300, "below_func": "log",    "below_target": 0.20,
                    "above_func": "sq",     "above_target": 3.60},
    "EGG":        {"base": 50,  "I0": 10000, "T": 332, "below_func": "linear", "below_target": 0.40,
                    "above_func": "log",    "above_target": 0.20},
    "MILK":       {"base": 160, "I0": 10000, "T": 122, "below_func": "sqrt",   "below_target": 0.60,
                    "above_func": "linear", "above_target": 1.60},
    "WOOL":       {"base": 200, "I0": 10000, "T": 105, "below_func": "log",    "below_target": 0.20,
                    "above_func": "sq",     "above_target": 3.20},
    "FERTILIZER": {"base": 100, "I0": 10000, "T": 200, "below_func": "linear", "below_target": 0.40,
                    "above_func": "linear", "above_target": 0.40},
}


def _shape(name, x):
    if name == "linear":
        return x
    if name == "sq":
        return x * x
    if name == "sqrt":
        return math.sqrt(x)
    if name == "log":
        return math.log(1 + x)
    if name == "log10":
        return math.log10(1 + x)
    raise ValueError(f"unknown shape function: {name}")


def _price_at(item, inv):
    """Replicates the game's price(inv) formula exactly."""
    p = MARKET_PARAMS.get(item)
    if p is None:
        return 1
    base, I0, T = p["base"], p["I0"], p["T"]
    if inv == I0:
        return base
    if inv < I0:
        func, target, sign, diff = p["below_func"], p["below_target"], 1, I0 - inv
    else:
        func, target, sign, diff = p["above_func"], p["above_target"], -1, inv - I0
    amp = target * base / _shape(func, T)
    price = base + sign * amp * _shape(func, diff)
    return max(1, round(price))


def _max_sell_qty(item, market_inv, available, max_drop_frac=0.15, hard_cap=25):
    """
    How many units of `item` we can sell this turn before the price would
    drop more than max_drop_frac below its current value (selling adds 1
    to market inventory per unit, same as the game's one-at-a-time model).
    Reads live market inventory from obs each turn, so it's inherently
    self-correcting against the opponent's sells too -- no need to track
    our own sell history separately.
    """
    if item not in MARKET_PARAMS or available <= 0:
        return 0
    current_price = _price_at(item, market_inv)
    floor_price = max(1, current_price * (1 - max_drop_frac))
    qty = 0
    sim_inv = market_inv
    while qty < min(available, hard_cap):
        sim_inv += 1
        if _price_at(item, sim_inv) < floor_price:
            break
        qty += 1
    return qty


def _max_buy_qty(item, market_inv, money, reserve, max_rise_frac=0.15, hard_cap=25):
    """Symmetric guard for BUY_PRODUCT: stop buying once price rises too much."""
    if item not in MARKET_PARAMS:
        return 0
    current_price = _price_at(item, market_inv)
    ceiling_price = current_price * (1 + max_rise_frac)
    budget = money - reserve
    qty = 0
    sim_inv = market_inv
    while qty < hard_cap:
        p = _price_at(item, sim_inv - 1) if sim_inv > 0 else current_price
        if p > ceiling_price or p > budget:
            break
        budget -= p
        sim_inv -= 1
        qty += 1
    return qty


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


def _nearest_shed_tile(pos):
    return min(SHED_TILES, key=lambda t: _manhattan(pos, t))


# ---------------------------------------------------------------------------
# Task queue
# ---------------------------------------------------------------------------

def _build_tasks(obs, me, plot_cap):
    step = obs.get("step", obs["day"] * TURNS_PER_DAY + obs["hour"])
    day = obs["day"]
    tiles = me["tiles"]
    board_size = len(tiles)
    half = board_size // 2
    unlocked = set(me["unlocked_quadrants"])

    active_plots = 0
    structure_counts = {"COOP": 0, "PASTURE": 0}
    animal_counts = {a: 0 for a in ANIMAL_CONFIG}

    for y in range(board_size):
        for x in range(board_size):
            t = tiles[y][x]
            if not isinstance(t, dict):
                continue
            if t.get("kind") == "PLANT":
                active_plots += 1
            elif t.get("kind") in ("COOP", "PASTURE"):
                structure_counts[t["kind"]] += 1
                animal = t.get("animal")
                if animal in animal_counts:
                    animal_counts[animal] += 1

    tasks = []
    for y in range(board_size):
        row = tiles[y]
        for x in range(board_size):
            if _quadrant_of(x, y, half) not in unlocked:
                continue
            tile = row[x]
            pos = (x, y)

            if tile is None:
                needed_structure = None
                for structure, target in STRUCTURE_TARGETS.items():
                    if structure_counts[structure] < target:
                        needed_structure = structure
                        break

                if needed_structure is not None:
                    tasks.append({
                        "pos": pos,
                        "action": ["BUILD_COOP" if needed_structure == "COOP" else "BUILD_PASTURE"],
                        "priority": PRIORITY_BUILD_STRUCTURE,
                    })
                    structure_counts[needed_structure] += 1
                    continue

                if active_plots >= plot_cap:
                    continue
                crop = CROP_ROTATION[(x * board_size + y) % len(CROP_ROTATION)]
                tasks.append({"pos": pos, "action": ["PLANT", crop], "priority": PRIORITY_PLANT})
                active_plots += 1
                continue

            if not isinstance(tile, dict):
                continue
            kind = tile.get("kind")

            if kind == "WEED":
                tasks.append({"pos": pos, "action": ["DIG"], "priority": PRIORITY_WEED})
                continue

            if kind == "PLANT":
                crop = tile["crop"]
                cfg = CROP_CONFIG.get(crop)
                if cfg is None:
                    continue
                yield_units = tile.get("yield_units", 0)
                watered = tile.get("watered_today", False)
                lifespan_step = tile.get("max_lifespan_step", -1)
                decaying_soon = lifespan_step != -1 and step >= lifespan_step - HARVEST_LEAD_TURNS
                fertilized_until = tile.get("fertilized_until_day", -1)
                age = day - tile.get("planted_day", day)

                if yield_units > 0 and decaying_soon:
                    tasks.append({"pos": pos, "action": ["HARVEST"], "priority": PRIORITY_URGENT_HARVEST})
                elif yield_units > 0 and cfg["ongoing"]:
                    tasks.append({"pos": pos, "action": ["HARVEST"], "priority": PRIORITY_HARVEST_ONGOING})
                elif not watered:
                    cu = tile.get("consecutive_unwatered", 0)
                    tasks.append({"pos": pos, "action": ["WATER"],
                                   "priority": PRIORITY_WATER_BASE + cu * 20})
                elif cfg["ongoing"] and fertilized_until < day:
                    # Doubling only triggers if fertilize + water land the same
                    # day -- already watered today, so fertilize now.
                    tasks.append({"pos": pos, "action": ["FERTILIZE"],
                                   "priority": PRIORITY_FERTILIZE_ONGOING,
                                   "requires_carry": "FERTILIZER", "carry_qty": FERTILIZER_FETCH_QTY})
                elif (not cfg["ongoing"] and fertilized_until < day
                      and cfg["bonus_window"][0] <= age <= cfg["bonus_window"][1]):
                    tasks.append({"pos": pos, "action": ["FERTILIZE"],
                                   "priority": PRIORITY_FERTILIZE_ONETIME,
                                   "requires_carry": "FERTILIZER", "carry_qty": FERTILIZER_FETCH_QTY})
                continue

            if kind in ("COOP", "PASTURE"):
                animal = tile.get("animal")
                if animal is None:
                    candidates = [a for a, cfg in ANIMAL_CONFIG.items()
                                  if cfg["structure"] == kind and animal_counts[a] < ANIMAL_TARGETS[a]]
                    if candidates:
                        target_animal = candidates[0]
                        tasks.append({"pos": pos, "action": ["PLACE", target_animal],
                                       "priority": PRIORITY_PLACE_ANIMAL,
                                       "requires_carry": target_animal, "carry_qty": 1})
                    continue

                yield_units = tile.get("yield_units", 0)
                fed_today = tile.get("fed_today", False)
                consecutive_unfed = tile.get("consecutive_unfed", 0)
                cared_today = tile.get("cared_today", False)
                fertilizer_available = tile.get("fertilizer_available", False)

                if not fed_today:
                    tasks.append({"pos": pos, "action": ["FEED"],
                                   "priority": PRIORITY_FEED + consecutive_unfed * 20,
                                   "requires_carry": "WHEAT", "carry_qty": 1})
                elif yield_units > 0:
                    tasks.append({"pos": pos, "action": ["HARVEST"], "priority": PRIORITY_ANIMAL_HARVEST})
                elif not cared_today:
                    tasks.append({"pos": pos, "action": ["CARE"], "priority": PRIORITY_CARE})
                elif fertilizer_available:
                    tasks.append({"pos": pos, "action": ["COLLECT_FERTILIZER"],
                                   "priority": PRIORITY_COLLECT_FERTILIZER})
                continue

    tasks.sort(key=lambda t: -t["priority"])
    return tasks


# ---------------------------------------------------------------------------
# Unit scheduling -- carry-aware
# ---------------------------------------------------------------------------

def _carried_count(private, unit_index, item):
    inventories = private.get("inventories", [])
    if unit_index >= len(inventories) or not isinstance(inventories[unit_index], dict):
        return 0
    return inventories[unit_index].get(item, 0)


def _assign_tasks(units, tasks, private, shed):
    remaining = list(units)
    name_to_index = {name: i for i, (name, _pos) in enumerate(units)}
    assignment = {}

    for task in tasks:
        if not remaining:
            break

        req_item = task.get("requires_carry")
        if req_item is None:
            best = min(remaining, key=lambda u: _manhattan(u[1], task["pos"]))
            assignment[best[0]] = task
            remaining.remove(best)
            continue

        req_qty = task.get("carry_qty", 1)
        carriers = [u for u in remaining
                    if _carried_count(private, name_to_index[u[0]], req_item) >= req_qty]

        if carriers:
            best = min(carriers, key=lambda u: _manhattan(u[1], task["pos"]))
            assignment[best[0]] = task
            remaining.remove(best)
            continue

        if shed.get(req_item, 0) < req_qty:
            continue

        best = min(remaining, key=lambda u: _manhattan(u[1], _nearest_shed_tile(u[1])))
        fetch_task = {
            "pos": _nearest_shed_tile(best[1]),
            "action": ["PICKUP", req_item, req_qty],
            "priority": task["priority"],
        }
        assignment[best[0]] = fetch_task
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
# Daily strategist -- hiring + land
# ---------------------------------------------------------------------------
FARM_HAND_COST_MULT = 1
HIRE_COST_CEILING = 13
MAX_HIRES_PER_DAY = 7
CASH_RESERVE = 200

LAND_ORDER = ["NE", "SW", "SE"]
LAND_COST = {"NE": 1000, "SW": 2000, "SE": 4000}
LAND_BUFFER = 500


def _fib(n):
    a, b = 1, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def _plan_hires(money, hires_today):
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
    for quadrant in LAND_ORDER:
        if quadrant in unlocked:
            continue
        cost = LAND_COST[quadrant]
        if money - cost >= CASH_RESERVE + LAND_BUFFER:
            return quadrant
        return None
    return None


# ---------------------------------------------------------------------------
# Market order builder
# ---------------------------------------------------------------------------
MAX_PRICE_DROP_FRAC = 0.15  # don't let our own selling crater price >15% in one turn
MAX_PRICE_RISE_FRAC = 0.15  # symmetric guard for buying


def _animal_shortfall(me):
    board = me["tiles"]
    placed = {a: 0 for a in ANIMAL_CONFIG}
    for row in board:
        for t in row:
            if isinstance(t, dict) and t.get("kind") in ("COOP", "PASTURE"):
                a = t.get("animal")
                if a in placed:
                    placed[a] += 1
    return placed


def _build_market_orders(obs, me, private, market, step):
    shed = private.get("shed", {})
    seeds = private.get("seeds", {})
    money = me["money"]
    prices = market.get("prices", {})
    inventory = market.get("inventory", {})
    unlocked = set(me["unlocked_quadrants"])
    hires_today = me.get("hires_today", 0)

    orders = []

    # --- Land purchase --------------------------------------------------
    quadrant = _plan_land_purchase(unlocked, money)
    if quadrant is not None:
        orders.append(["BUY_LAND"])
        money -= LAND_COST[quadrant]

    # --- Hire hands -------------------------------------------------------
    n_hires = _plan_hires(money, hires_today)
    for _ in range(n_hires):
        if len(orders) >= 10:
            break
        orders.append(["HIRE"])
        money -= FARM_HAND_COST_MULT * _fib(hires_today)
        hires_today += 1

    # --- Buy animals still short of target ---------------------------------
    placed = _animal_shortfall(me)
    for animal, target in ANIMAL_TARGETS.items():
        if len(orders) >= 10:
            break
        already_owned = placed.get(animal, 0) + shed.get(animal, 0)
        cost = ANIMAL_CONFIG[animal]["cost"]
        if already_owned < target and money - CASH_RESERVE >= cost:
            orders.append(["BUY_ANIMAL", animal, 1])
            money -= cost

    # --- Price-aware drip-sell of shed contents -----------------------------
    sellable = [(item, count) for item, count in shed.items()
                if count > 0 and item not in ANIMAL_TARGETS]
    sellable.sort(key=lambda kv: -prices.get(kv[0], 1))

    for item, count in sellable:
        if len(orders) >= 10:
            break
        available = count
        if item == "WHEAT":
            available = max(0, count - WHEAT_FEED_BUFFER)  # protect animal feed stock

        market_inv = inventory.get(item, MARKET_PARAMS.get(item, {}).get("I0", 10000))
        qty = _max_sell_qty(item, market_inv, available, max_drop_frac=MAX_PRICE_DROP_FRAC)
        if qty <= 0:
            continue
        orders.append(["SELL", item, qty])

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

    # --- Keep wheat feed buffer topped up via BUY_PRODUCT --------------------
    if len(orders) < 10 and shed.get("WHEAT", 0) < WHEAT_FEED_BUFFER:
        need = WHEAT_FEED_BUFFER - shed.get("WHEAT", 0)
        market_inv = inventory.get("WHEAT", MARKET_PARAMS["WHEAT"]["I0"])
        buy_n = min(need, _max_buy_qty("WHEAT", market_inv, money, CASH_RESERVE,
                                         max_rise_frac=MAX_PRICE_RISE_FRAC))
        if buy_n > 0:
            orders.append(["BUY_PRODUCT", "WHEAT", buy_n])

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
    assignment = _assign_tasks(units, tasks, private, private.get("shed", {}))
    unit_actions = _build_unit_actions(units, assignment)

    farmer_action = unit_actions.get("farmer", ["PASS"])
    hand_actions = [unit_actions[f"hand{i}"] for i in range(len(me.get("hands", [])))]

    market_orders = _build_market_orders(obs, me, private, market, step)

    return {"farmer": farmer_action, "hands": hand_actions, "market": market_orders}


if __name__ == "__main__":
    import json
    from kaggle_environments import make

    NUM_PLAYS = 100
    TURNS_PER_PLAY = 720
    OUTPUT_FILE = "results.json"

    # rewards[player_index] = list of per-play rewards
    rewards = {0: [], 1: []}
    plays_log = []  # one dict per play

    print(f"Running {NUM_PLAYS} plays ({TURNS_PER_PLAY} turns each)...\n")
    print(f"{'Play':>5}  {'P0 Reward':>12}  {'P1 Reward':>12}")
    print("-" * 35)

    for play in range(1, NUM_PLAYS + 1):
        env = make("kaggriculture", configuration={"episodeSteps": TURNS_PER_PLAY}, debug=False)
        env.run([agent, "starter"])
        final = env.steps[-1]

        r0 = final[0].reward if final[0].reward is not None else 0.0
        r1 = final[1].reward if final[1].reward is not None else 0.0

        rewards[0].append(r0)
        rewards[1].append(r1)
        plays_log.append({"play": play, "turns": TURNS_PER_PLAY,
                          "player_0": r0, "player_1": r1})

        print(f"{play:>5}  {r0:>12.1f}  {r1:>12.1f}")

    # ── Summary ──────────────────────────────────────────────────────────────
    summary = {}
    print("\n" + "=" * 55)
    print(f"{'Summary after':} {NUM_PLAYS} plays  ({TURNS_PER_PLAY} turns/play)")
    print("=" * 55)
    for pid in (0, 1):
        r = rewards[pid]
        avg = sum(r) / len(r)
        summary[f"player_{pid}"] = {"avg": round(avg, 2), "min": min(r), "max": max(r)}
        print(f"  Player {pid}:  avg={avg:>10.1f}  min={min(r):>10.1f}  max={max(r):>10.1f}")
    print("=" * 55)

    # ── Write JSON ────────────────────────────────────────────────────────────
    output = {
        "config": {"num_plays": NUM_PLAYS, "turns_per_play": TURNS_PER_PLAY},
        "plays": plays_log,
        "summary": summary,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to {OUTPUT_FILE}")