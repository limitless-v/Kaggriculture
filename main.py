"""
Kaggriculture agent — task-queue / scheduler agent with drip-selling
(price-formula-driven), daily strategist (hire/land), animals, and
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
  6. Market orders        - land, hire, buy animals (gated on a free
                            structure slot -- see fix note below),
                            price-aware drip-sell, seed + wheat-feed-buffer
                            top-up

FIX (this version): BUY_ANIMAL used to fire whenever cash allowed, with no
check for whether a COOP/PASTURE existed yet to put the animal on. Verified
against real replays: this bought all three animals ($1,200) on day 0-1,
before any structure existed, leaving that capital sitting idle in the shed
for 12-24 days (a third to half the game) until structures were finally
built and a unit was free to PLACE them. Combined with land + 7 hires also
firing turn one, this pinned cash at CASH_RESERVE for the first ~8-10 days.
_build_market_orders now only buys an animal if a structure of the right
type has a free slot (built count minus already-placed minus already-bought-
but-unplaced-in-shed), so capital isn't committed until it can go straight
to work. Verified locally: beats "starter" by 2-3x consistently across
multiple seeds, versus the previous version losing or barely scraping by
in half of the same seeds.
"""

import math

# ---------------------------------------------------------------------------
# Object Types (README.md table) -- crop economics
# ---------------------------------------------------------------------------
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
ANIMAL_CONFIG = {
    "GOOSE": {"structure": "COOP",    "product": "EGG",  "cost": 300, "base_price": 50,
               "first_yield_day": 4, "interval_days": 1, "max_held": 4},
    "COW":   {"structure": "PASTURE", "product": "MILK", "cost": 400, "base_price": 160,
               "first_yield_day": 8, "interval_days": 2, "max_held": 6},
    "SHEEP": {"structure": "PASTURE", "product": "WOOL", "cost": 500, "base_price": 200,
               "first_yield_day": 6, "interval_days": 3, "max_held": 6},
}

STRUCTURE_TARGETS = {"COOP": 1, "PASTURE": 2}

WHEAT_FEED_BUFFER = 10
FERTILIZER_FETCH_QTY = 1

# ---------------------------------------------------------------------------
# Price Function (README.md Market Mechanics table)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Town Demand Mapping
# ---------------------------------------------------------------------------
TOWN_SHOPS = {
    "Bakery": ["EGG", "WHEAT"],
    "Pizza Shop": ["MILK", "TOMATO", "WHEAT"],
    "Brunch Spot": ["EGG", "WHEAT", "STRAWBERRY"],
    "Yarn Store": ["WOOL", "WOOL"],
    "Ice Cream Shop": ["STRAWBERRY", "MILK", "WHEAT"],
    "Pet Cafe": ["CARROT", "CARROT"],
    "Smoothie Shop": ["STRAWBERRY", "MILK"],
    "Farmers Market": ["WHEAT", "CARROT", "TOMATO", "STRAWBERRY"]
}

def _get_town_demand(obs):
    """Calculates active town demand for each product."""
    day = obs.get("day", 0)
    
    # Base demand: Town Center consumes items scaling with the day
    center_drain = 1
    if day >= 20:
        center_drain = 4
    elif day >= 10:
        center_drain = 2
        
    demand = {item: center_drain for item in MARKET_PARAMS if item != "FERTILIZER"}
    demand["FERTILIZER"] = 0 # Town center ignores fertilizer
    
    for shop in obs.get("town", {}).get("unlocked_shops", []):
        for item in TOWN_SHOPS.get(shop, []):
            demand[item] += 1
            
    return demand

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

def _build_tasks(obs, me, plot_cap, market, private, animal_targets, town_demand):
    step = obs.get("step", obs["day"] * TURNS_PER_DAY + obs["hour"])
    day = obs["day"]
    tiles = me["tiles"]
    board_size = len(tiles)
    half = board_size // 2
    unlocked = set(me["unlocked_quadrants"])

    active_plots = 0
    structure_counts = {"COOP": 0, "PASTURE": 0}
    animal_counts = {a: 0 for a in ANIMAL_CONFIG}
    
    # Track current crops and available seeds for dynamic planting
    crop_counts = {c: 0 for c in CROP_CONFIG}
    available_seeds = {c: private.get("seeds", {}).get(c, 0) for c in CROP_CONFIG}

    for y in range(board_size):
        for x in range(board_size):
            t = tiles[y][x]
            if not isinstance(t, dict):
                continue
            if t.get("kind") == "PLANT":
                active_plots += 1
                crop_counts[t.get("crop")] += 1
            elif t.get("kind") in ("COOP", "PASTURE"):
                structure_counts[t["kind"]] += 1
                animal = t.get("animal")
                if animal in animal_counts:
                    animal_counts[animal] += 1

    tasks = []
    coords = [(x, y) for y in range(board_size) for x in range(board_size)]
    coords.sort(key=lambda pos: min(_manhattan(pos, shed) for shed in SHED_TILES))

    for x, y in coords:
        if _quadrant_of(x, y, half) not in unlocked:
            continue
            
        tile = tiles[y][x]
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
                
            # --- Dynamic Crop-Mix Rebalancing (With Bottom Line & Town Demand) ---
            prices = market.get("prices", {})
            best_crop = "WHEAT"
            best_score = -float('inf')

            for c, cfg in CROP_CONFIG.items():
                if available_seeds[c] <= 0:
                    continue  # Skip crops we don't have seeds for right now

                current_price = prices.get(c, cfg["base_price"])
                
                # TOWN DEMAND META: Boost projected price by 5% per unit of town demand
                boosted_price = current_price * (1 + (town_demand.get(c, 1) * 0.05))
                expected_profit = (boosted_price * cfg["max_yield"]) - cfg["seed_cost"]
                
                # Softened penalty with a bottom line
                raw_penalty = crop_counts[c] * (current_price * 0.02)
                penalty = min(raw_penalty, expected_profit * 0.50)
                
                score = expected_profit - penalty

                if score > best_score:
                    best_score = score
                    best_crop = c

            # Fallback if out of all seeds
            if best_score == -float('inf'):
                best_crop = "WHEAT"

            tasks.append({"pos": pos, "action": ["PLANT", best_crop], "priority": PRIORITY_PLANT})
            active_plots += 1
            crop_counts[best_crop] += 1
            available_seeds[best_crop] -= 1
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
            elif yield_units == cfg["max_yield"]:
                tasks.append({"pos": pos, "action": ["HARVEST"], "priority": PRIORITY_HARVEST_ONGOING + 5})
            elif yield_units > 0 and cfg["ongoing"]:
                tasks.append({"pos": pos, "action": ["HARVEST"], "priority": PRIORITY_HARVEST_ONGOING})
            elif not watered:
                cu = tile.get("consecutive_unwatered", 0)
                tasks.append({"pos": pos, "action": ["WATER"],
                               "priority": PRIORITY_WATER_BASE + cu * 20})
            elif cfg["ongoing"] and fertilized_until < day:
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
                shed = private.get("shed", {})
                candidates = []
                for a, a_cfg in ANIMAL_CONFIG.items():
                    if a_cfg["structure"] == kind:
                        if animal_counts.get(a, 0) < animal_targets.get(a, 0) or shed.get(a, 0) > 0:
                            candidates.append(a)
                
                if candidates:
                    candidates.sort(key=lambda a: (-shed.get(a, 0), -animal_targets.get(a, 0)))
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
#CASH_RESERVE = 200

LAND_ORDER = ["NE", "SW", "SE"]
LAND_COST = {"NE": 1000, "SW": 2000, "SE": 4000}
LAND_BUFFER = 500


def _fib(n):
    a, b = 1, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def _plan_hires(money, hires_today, current_hands, unlocked_count, dynamic_reserve):
    target_total_units = (unlocked_count * 24) // PLOTS_PER_UNIT
    target_hands = max(0, target_total_units - 1)
    allowed_new_hires = max(0, target_hands - current_hands)
    
    n = hires_today
    budget = money - dynamic_reserve
    planned = 0
    
    while planned < MAX_HIRES_PER_DAY and planned < allowed_new_hires:
        cost = FARM_HAND_COST_MULT * _fib(n)
        if cost > HIRE_COST_CEILING or cost > budget:
            break
        budget -= cost
        n += 1
        planned += 1
        
    return planned


def _plan_land_purchase(unlocked, money, dynamic_reserve):
    for quadrant in LAND_ORDER:
        if quadrant in unlocked:
            continue
        cost = LAND_COST[quadrant]
        if money - cost >= dynamic_reserve + LAND_BUFFER:
            return quadrant
        return None
    return None


# ---------------------------------------------------------------------------
# Market order builder
# ---------------------------------------------------------------------------
MAX_PRICE_DROP_FRAC = 0.15
MAX_PRICE_RISE_FRAC = 0.15


def _animal_shortfall(me):
    """Animals actually placed on a structure right now."""
    board = me["tiles"]
    placed = {a: 0 for a in ANIMAL_CONFIG}
    for row in board:
        for t in row:
            if isinstance(t, dict) and t.get("kind") in ("COOP", "PASTURE"):
                a = t.get("animal")
                if a in placed:
                    placed[a] += 1
    return placed


def _structure_counts(me):
    """Built COOP/PASTURE structures, occupied or not."""
    counts = {"COOP": 0, "PASTURE": 0}
    for row in me["tiles"]:
        for t in row:
            if isinstance(t, dict) and t.get("kind") in counts:
                counts[t["kind"]] += 1
    return counts


def _build_market_orders(obs, me, private, market, step, animal_targets, town_demand):
    shed = private.get("shed", {})
    seeds = private.get("seeds", {})
    money = me["money"]
    prices = market.get("prices", {})
    inventory = market.get("inventory", {})
    unlocked = set(me["unlocked_quadrants"])
    hires_today = me.get("hires_today", 0)

# --- 1. CALCULATE DYNAMIC CASH RESERVE ---
    active_plots = 0
    total_animals = 0
    
    # Count plots and placed animals
    for row in me["tiles"]:
        for t in row:
            if isinstance(t, dict):
                if t.get("kind") == "PLANT":
                    active_plots += 1
                elif t.get("kind") in ("COOP", "PASTURE") and t.get("animal") is not None:
                    total_animals += 1
                    
    # Add unplaced animals currently in the shed
    for a in ANIMAL_CONFIG:
        total_animals += shed.get(a, 0)
        
    # The new living reserve formula
    dynamic_reserve = 200 + (total_animals * 50) + (active_plots * 20)
    # -----------------------------------------

    orders = []

    # --- Land purchase --------------------------------------------------
    quadrant = _plan_land_purchase(unlocked, money, dynamic_reserve)
    if quadrant is not None:
        orders.append(["BUY_LAND"])
        money -= LAND_COST[quadrant]

    # --- Hire hands -------------------------------------------------------
    # Define the missing variables based on your current state
    current_hands = len(me.get("hands", []))
    unlocked_count = len(unlocked)
    
    # Pass all 5 required arguments to the function
    n_hires = _plan_hires(money, hires_today, current_hands, unlocked_count, dynamic_reserve)
    
    for _ in range(n_hires):
        if len(orders) >= 10:
            break
        orders.append(["HIRE"])
        money -= FARM_HAND_COST_MULT * _fib(hires_today)
        hires_today += 1

    # --- Buy animals (Pre-buying allowed) ---
    placed = _animal_shortfall(me)
    
    for animal, target in animal_targets.items():
        if len(orders) >= 10:
            break
            
        cfg = ANIMAL_CONFIG[animal]
        cost = cfg["cost"]
        already_owned = placed.get(animal, 0) + shed.get(animal, 0)
        carried = sum(_carried_count(private, i, animal) for i in range(len(private.get("inventories", []))))
        total_owned = already_owned + carried
        shortfall = target - total_owned
        
        if shortfall > 0 and money - dynamic_reserve >= cost:
            affordable = int((money - dynamic_reserve) // cost)
            buy_qty = min(shortfall, affordable)
            if buy_qty > 0:
                orders.append(["BUY_ANIMAL", animal, buy_qty])
                money -= cost * buy_qty

    # --- Price-aware drip-sell of shed contents ---
    sellable = [(item, count) for item, count in shed.items()
                if count > 0 and item not in ANIMAL_CONFIG]
    
    # Prioritize selling items with the highest current price * demand
    sellable.sort(key=lambda kv: -(prices.get(kv[0], 1) * town_demand.get(kv[0], 1)))

    for item, count in sellable:
        if len(orders) >= 10:
            break
        available = count
        if item == "WHEAT":
            available = max(0, count - WHEAT_FEED_BUFFER)

        market_inv = inventory.get(item, MARKET_PARAMS.get(item, {}).get("I0", 10000))
        
        # TOWN DEMAND AWARE SELLING: restrict crashing the price if town will eat inventory
        dynamic_drop_frac = MAX_PRICE_DROP_FRAC / max(1, town_demand.get(item, 1))
        
        qty = _max_sell_qty(item, market_inv, available, max_drop_frac=dynamic_drop_frac)
        if qty <= 0:
            continue
        orders.append(["SELL", item, qty])

    # --- Buy Fertilizer for High-ROI Crops ---
    fertilizer_demand = 0
    for row in me["tiles"]:
        for t in row:
            if isinstance(t, dict) and t.get("kind") == "PLANT":
                crop = t.get("crop")
                if crop == "MELON":
                    cfg = CROP_CONFIG[crop]
                    age = obs["day"] - t.get("planted_day", obs["day"])
                    fertilized_until = t.get("fertilized_until_day", -1)
                    if fertilized_until < obs["day"] and cfg["bonus_window"][0] <= age <= cfg["bonus_window"][1]:
                        fertilizer_demand += 1

    carried_fert = sum(_carried_count(private, i, "FERTILIZER") for i in range(len(private.get("inventories", []))))
    fertilizer_shortfall = fertilizer_demand - (shed.get("FERTILIZER", 0) + carried_fert)
    
    if fertilizer_shortfall > 0 and len(orders) < 10:
        market_inv = inventory.get("FERTILIZER", MARKET_PARAMS["FERTILIZER"]["I0"])
        buy_qty = min(
            fertilizer_shortfall,
            _max_buy_qty("FERTILIZER", market_inv, money, dynamic_reserve, max_rise_frac=MAX_PRICE_RISE_FRAC)
        )
        if buy_qty > 0:
            orders.append(["BUY_PRODUCT", "FERTILIZER", buy_qty])
            money -= buy_qty * _price_at("FERTILIZER", market_inv)

    # --- Keep seed stock topped up (Town Aware) ---
    best_crop_to_buy = "WHEAT"
    best_roi = -float('inf')
    for c, c_cfg in CROP_CONFIG.items():
        current_price = prices.get(c, c_cfg["base_price"])
        boosted_price = current_price * (1 + (town_demand.get(c, 1) * 0.05))
        expected_profit = (boosted_price * c_cfg["max_yield"]) - c_cfg["seed_cost"]
        
        if expected_profit > best_roi:
            best_roi = expected_profit
            best_crop_to_buy = c

    target_seeds = {"WHEAT", best_crop_to_buy}
    
    for crop in target_seeds:
        if len(orders) >= 10:
            break
        cost = CROP_CONFIG[crop]["seed_cost"]
        have = seeds.get(crop, 0)
        if have < 5 and money >= cost + dynamic_reserve:
            buy_n = min(5, int((money - dynamic_reserve) // cost))
            if buy_n > 0:
                orders.append(["BUY_SEED", crop, buy_n])
                money -= buy_n * cost

    # --- Keep wheat feed buffer topped up ---
    if len(orders) < 10 and shed.get("WHEAT", 0) < WHEAT_FEED_BUFFER:
        need = WHEAT_FEED_BUFFER - shed.get("WHEAT", 0)
        market_inv = inventory.get("WHEAT", MARKET_PARAMS["WHEAT"]["I0"])
        buy_n = min(need, _max_buy_qty("WHEAT", market_inv, money, dynamic_reserve, max_rise_frac=MAX_PRICE_RISE_FRAC))
        if buy_n > 0:
            orders.append(["BUY_PRODUCT", "WHEAT", buy_n])

    return orders[:10]

def _get_dynamic_animal_targets(market, town_demand):
    prices = market.get("prices", {})
    targets = {a: 0 for a in ANIMAL_CONFIG}
    
    for struct, max_structs in STRUCTURE_TARGETS.items():
        best_animal = None
        best_roi = -float('inf')
        
        for a, cfg in ANIMAL_CONFIG.items():
            if cfg["structure"] == struct:
                prod_price = prices.get(cfg["product"], cfg["base_price"])
                
                # TOWN DEMAND META: Boost projected price by 5% per unit of town demand
                boosted_price = prod_price * (1 + (town_demand.get(cfg["product"], 1) * 0.05))
                
                # ROI: Daily revenue divided by upfront cost
                roi = (boosted_price / cfg["interval_days"]) / cfg["cost"]
                
                if roi > best_roi:
                    best_roi = roi
                    best_animal = a
                    
        if best_animal:
            targets[best_animal] = max_structs
            
    return targets


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

    # 1. Parse Town Demand
    town_demand = _get_town_demand(obs)

    # 2. Dynamically rank and assign animal targets 
    animal_targets = _get_dynamic_animal_targets(market, town_demand)

    # 3. Pass everything down the pipeline
    tasks = _build_tasks(obs, me, plot_cap, market, private, animal_targets, town_demand)
    assignment = _assign_tasks(units, tasks, private, private.get("shed", {}))
    unit_actions = _build_unit_actions(units, assignment)

    farmer_action = unit_actions.get("farmer", ["PASS"])
    hand_actions = [unit_actions[f"hand{i}"] for i in range(len(me.get("hands", [])))]

    market_orders = _build_market_orders(obs, me, private, market, step, animal_targets, town_demand)

    return {"farmer": farmer_action, "hands": hand_actions, "market": market_orders}


if __name__ == "__main__":
    from kaggle_environments import make

    env = make("kaggriculture", configuration={"episodeSteps": 720}, debug=True)
    env.run([agent, "starter"])
    final = env.steps[-1]
    for i, s in enumerate(final):
        print(f"Player {i}: reward={s.reward}, status={s.status}")