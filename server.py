import os
import asyncio
import json
import random
from aiohttp import web, WSMsgType

GREEN = 'good'
RED = 'bad'
PURPLE = 'wild'

GOOD_NAMES = [
    "Secured the NSU Club Room", "Meek App Beta Launched", "Qualifie Pitch Nailed",
    "Free Food at TEDx", "Extension Granted", "Memoir Apparels Sold Out"
]
BAD_NAMES = [
    "Bashundhara Gridlock", "Stuck Grading TA Sheets",
    "Wi-Fi Died", "CompetenC Crashed", "Kitty Imports Delayed"
]

clients = {}   # ws -> {"name": str, "ready": bool}
game_state = None
kick_votes = {}   # target_name -> set of voter names

# ── Win target by player count ────────────────────────────────────────────────


def win_target(num_players):
    return 2 if num_players >= 7 else 3

# ── Deck construction (simplified: 4p=50, 5-6p=70, 7-10p=100) ────────────────
# wild split follows the largest matching V3 table entry
# (save, nuke, net, drama, coffee, dean)


def build_deck(num_players):
    n = num_players
    if n <= 4:
        good, bad = 24, 15
        wild_counts = (2, 2, 2, 2, 2, 1)   # 11 wilds → 50 total
    elif n <= 6:
        good, bad = 32, 21
        wild_counts = (2, 3, 3, 4, 3, 2)   # 17 wilds → 70 total
    else:
        good, bad = 46, 30
        wild_counts = (4, 4, 4, 5, 4, 3)   # 24 wilds → 100 total

    save, nuke, net, drama, coffee, dean = wild_counts
    deck = []
    for _ in range(good):
        deck.append({"type": "Good",  "name": random.choice(
            GOOD_NAMES), "color": GREEN})
    for _ in range(bad):
        deck.append(
            {"type": "Bad",   "name": random.choice(BAD_NAMES),  "color": RED})
    for _ in range(save):
        deck.append(
            {"type": "Wild",  "name": "Miracle Rescheduling",    "color": PURPLE})
    for _ in range(nuke):
        deck.append(
            {"type": "Wild",  "name": "Flaked Last Minute",      "color": PURPLE})
    for _ in range(net):
        deck.append(
            {"type": "Wild",  "name": "The Networker",           "color": PURPLE})
    for _ in range(drama):
        deck.append(
            {"type": "Wild",  "name": "Group Chat Drama",        "color": PURPLE})
    for _ in range(coffee):
        deck.append(
            {"type": "Wild",  "name": "Coffee Run",              "color": PURPLE})
    for _ in range(dean):
        deck.append(
            {"type": "Wild",  "name": "Dean's Office",           "color": PURPLE})
    random.shuffle(deck)
    return deck


def refill_deck(gs):
    """Merge discard + used_pile back into the draw deck and reshuffle to full size."""
    pool = gs["discard"][:] + gs["used_pile"][:]
    if not pool:
        return
    random.shuffle(pool)
    gs["deck"] = pool
    gs["discard"] = []
    gs["used_pile"] = []
    gs["reshuffle_count"] = gs.get("reshuffle_count", 0) + 1
    gs["log"].append(
        f"♻️ Deck below 3 — discard + used cards ({len(gs['deck'])}) reshuffled back in! "
        f"Reshuffle #{gs['reshuffle_count']}.")


def draw_card(gs):
    """Draw one card, refilling from discard+used when deck drops below 3."""
    if len(gs["deck"]) < 3:
        refill_deck(gs)
    return gs["deck"].pop() if gs["deck"] else None


def discard_card(gs, card):
    """Send a card to the discard pile (end-of-turn excess, Group Chat Drama, shield)."""
    gs["discard"].append(card)


def use_card(gs, card):
    """Card was consumed by a plan action — goes to used_pile, hidden from players."""
    gs["used_pile"].append(card)


def new_game_state(player_names):
    n = len(player_names)
    deck = build_deck(n)
    target = win_target(n)
    players = []
    for name in player_names:
        hand = []
        for _ in range(5):
            if deck:
                hand.append(deck.pop())
        players.append({
            "name": name,
            "hand": hand,
            "desk": {"good": 0, "status": "Empty"},   # personal desk
            "locked_count": 0,                         # this player's score
            "plays_left": 2,
            "has_drawn": False,
            "cards_played": 0,
            "shield_pending": False,   # Miracle Rescheduling out-of-turn flag
        })
    total = len(deck) + sum(len(p["hand"]) for p in players)
    full_deck_size = len(deck)   # cards remaining in draw pile after dealing
    return {
        "deck": deck,
        "discard": [],
        "players": players,
        "current_turn": 0,
        "phase": "playing",
        "win_target": target,
        "log": [f"🎓 NSU Hustle & Sabotage! {n} players · {total} cards · First to lock {target} projects wins!"],
        "pending_dean": None,
        # {"attacker": name, "target": name, "card": card, "card_idx": int}
        "pending_shield": None,
        # derived from len(discard) — kept for forward compat
        "cards_discarded": 0,
        "reshuffle_count": 0,   # how many times discard was reshuffled back
        # cards consumed by plan actions (hidden from clients)
        "used_pile": [],
        "full_deck_size": full_deck_size,  # target deck size on every reshuffle
    }


def check_win(gs):
    target = gs["win_target"]
    for p in gs["players"]:
        if p["locked_count"] >= target:
            gs["phase"] = "winner"
            gs["winner"] = p["name"]
            gs["log"].append(
                f"🏆 {p['name']} wins with {p['locked_count']} locked projects! Ultimate NSU Hustler!")
            return True
    return False


def check_desk_lock(gs, player):
    """Lock a player's desk if it has 3 good cards."""
    desk = player["desk"]
    if desk["good"] >= 3 and desk["status"] != "Locked":
        player["locked_count"] += 1
        desk["good"] = 3
        desk["status"] = "Locked"
        gs["log"].append(
            f"🔒 {player['name']} LOCKED a project! Score: {player['locked_count']}/{gs['win_target']}")
        return True
    return False


def public_state(gs, viewer_name=None):
    players_public = []
    for p in gs["players"]:
        is_viewer = p["name"] == viewer_name
        players_public.append({
            "name": p["name"],
            "hand_count": len(p["hand"]),
            "hand": p["hand"] if is_viewer else [],
            "desk": p["desk"],
            "locked_count": p["locked_count"],
            "plays_left": p["plays_left"],
            "has_drawn": p["has_drawn"],
            "cards_played": p.get("cards_played", 0) if is_viewer else 0,
        })
    kick_status = {k: list(v) for k, v in kick_votes.items()}
    return {
        "players": players_public,
        "current_turn": gs["current_turn"],
        "current_player": gs["players"][gs["current_turn"]]["name"],
        "phase": gs["phase"],
        "winner": gs.get("winner"),
        "win_target": gs["win_target"],
        "log": gs["log"][-25:],
        "deck_count": len(gs["deck"]),
        "discard_count": len(gs["discard"]),
        "cards_discarded": len(gs["discard"]),
        "reshuffle_count": gs.get("reshuffle_count", 0),
        "pending_dean": gs.get("pending_dean"),
        "pending_shield": gs.get("pending_shield"),
        "kick_votes": kick_status,
        "total_players": len(gs["players"]),
    }


def lobby_payload():
    game_active = game_state is not None and game_state["phase"] == "playing"
    players = [{"name": i["name"], "ready": i.get(
        "ready", False)} for i in clients.values()]
    all_ready = len(players) >= 4 and all(p["ready"] for p in players)
    return {
        "type": "lobby_update",
        "players": players,
        "game_active": game_active,
        "all_ready": all_ready,
    }


async def broadcast(msg_dict, exclude=None):
    for ws, info in list(clients.items()):
        if ws == exclude:
            continue
        try:
            payload = dict(msg_dict)
            if game_state and payload.get("type") == "state_update":
                payload["state"] = public_state(game_state, info["name"])
            await ws.send(json.dumps(payload))
        except:
            pass


async def broadcast_lobby():
    msg = lobby_payload()
    for ws in list(clients.keys()):
        try:
            await ws.send(json.dumps(msg))
        except:
            pass


async def send_to(ws, msg_dict):
    info = clients.get(ws, {})
    payload = dict(msg_dict)
    if game_state and payload.get("type") == "state_update":
        payload["state"] = public_state(game_state, info.get("name"))
    try:
        await ws.send(json.dumps(payload))
    except:
        pass


async def handle_message(ws, raw):
    global game_state, kick_votes
    msg = json.loads(raw)
    action = msg.get("action")
    gs = game_state

    # ── JOIN ──────────────────────────────────────────────────────────────────
    if action == "join":
        name = msg["name"].strip()[:20]
        if not name:
            await ws.send(json.dumps({"type": "error", "msg": "Name required"}))
            return
        if name in [i["name"] for i in clients.values()]:
            await ws.send(json.dumps({"type": "error", "msg": "Name taken"}))
            return
        if gs and gs["phase"] == "playing":
            await ws.send(json.dumps({"type": "error", "msg": "A game is already in progress."}))
            return
        clients[ws] = {"name": name, "ready": False}
        await broadcast_lobby()
        await ws.send(json.dumps({"type": "joined", "name": name}))
        return

    # ── TOGGLE READY ──────────────────────────────────────────────────────────
    if action == "toggle_ready":
        if ws not in clients:
            return
        clients[ws]["ready"] = not clients[ws].get("ready", False)
        await broadcast_lobby()
        return

    # ── CHAT ──────────────────────────────────────────────────────────────────
    if action == "chat":
        if ws not in clients:
            return
        text = msg.get("text", "").strip()[:200]
        if not text:
            return
        await broadcast({"type": "chat", "sender": clients[ws]["name"], "text": text})
        return

    # ── START GAME ────────────────────────────────────────────────────────────
    if action == "start_game":
        if len(clients) < 4:
            await send_to(ws, {"type": "error", "msg": "Need at least 4 players"})
            return
        not_ready = [i["name"]
                     for i in clients.values() if not i.get("ready", False)]
        if not_ready:
            await send_to(ws, {"type": "error", "msg": f"Not everyone is ready: {', '.join(not_ready)}"})
            return
        names = [i["name"] for i in clients.values()]
        game_state = new_game_state(names)
        kick_votes = {}
        for info in clients.values():
            info["ready"] = False
        await broadcast({"type": "game_started"})
        await broadcast({"type": "state_update"})
        return

    # ── REPLAY GAME (same players, fresh deck) ───────────────────────────────
    if action == "replay_game":
        if ws not in clients:
            return
        if len(clients) < 4:
            await send_to(ws, {"type": "error", "msg": "Need at least 4 players to replay"})
            return
        names = [i["name"] for i in clients.values()]
        game_state = new_game_state(names)
        kick_votes = {}
        for info in clients.values():
            info["ready"] = False
        await broadcast({"type": "game_started"})
        await broadcast({"type": "state_update"})
        return

    # ── EXIT TO LOBBY ─────────────────────────────────────────────────────────
    if action == "exit_to_lobby":
        if ws not in clients:
            return
        my_name = clients[ws]["name"]
        if gs:
            gs["players"] = [p for p in gs["players"] if p["name"] != my_name]
            gs["log"].append(f"🚪 {my_name} left the game.")
            if len(gs["players"]) < 4:
                gs["phase"] = "ended"
                gs["log"].append("Not enough players — game ended.")
                game_state = None
                kick_votes = {}
                await broadcast({"type": "game_ended"})
            else:
                gs["current_turn"] = gs["current_turn"] % len(gs["players"])
        clients[ws]["ready"] = False
        await ws.send(json.dumps({"type": "return_to_lobby"}))
        await broadcast_lobby()
        if game_state:
            await broadcast({"type": "state_update"})
        return

    # ── KICK VOTE ─────────────────────────────────────────────────────────────
    if action == "vote_kick":
        if ws not in clients or not gs:
            return
        voter = clients[ws]["name"]
        target = msg.get("target")
        if not target or target == voter:
            return
        if not any(p["name"] == target for p in gs["players"]):
            return
        if target not in kick_votes:
            kick_votes[target] = set()
        if voter in kick_votes[target]:
            kick_votes[target].discard(voter)
            gs["log"].append(f"🗳 {voter} withdrew kick vote for {target}.")
        else:
            kick_votes[target].add(voter)
            needed = max(2, len(gs["players"]) // 2 + 1)
            votes = len(kick_votes[target])
            gs["log"].append(
                f"🗳 {voter} voted to kick {target}. ({votes}/{needed})")
            if votes >= needed:
                gs["players"] = [p for p in gs["players"] if p["name"] != target]
                del kick_votes[target]
                gs["log"].append(f"🔨 {target} was kicked!")
                target_ws = next((w for w, i in clients.items()
                                 if i["name"] == target), None)
                if target_ws:
                    await target_ws.send(json.dumps({"type": "kicked"}))
                    del clients[target_ws]
                if len(gs["players"]) < 4:
                    game_state = None
                    kick_votes = {}
                    await broadcast({"type": "game_ended"})
                    await broadcast_lobby()
                    return
                else:
                    gs["current_turn"] = gs["current_turn"] % len(
                        gs["players"])
        await broadcast({"type": "state_update"})
        return

    if not gs or ws not in clients:
        return

    my_name = clients[ws]["name"]

    # ── SHIELD (OUT-OF-TURN Miracle Rescheduling) ─────────────────────────────
    if action == "play_shield":
        # Player responds to a pending attack on their desk
        pending = gs.get("pending_shield")
        if not pending or pending["target"] != my_name:
            return
        # Find Miracle Rescheduling in their hand
        me = next((p for p in gs["players"] if p["name"] == my_name), None)
        if not me:
            return
        shield_idx = next((i for i, c in enumerate(
            me["hand"]) if c["name"] == "Miracle Rescheduling"), None)
        if shield_idx is None:
            await send_to(ws, {"type": "error", "msg": "You don't have a Miracle Rescheduling card!"})
            return
        shield_card = me["hand"].pop(shield_idx)
        use_card(gs, shield_card)       # shield card consumed
        use_card(gs, pending["card"])   # blocked attack card consumed
        gs["pending_shield"] = None
        gs["log"].append(
            f"🛡️ {my_name} blocked the attack with Miracle Rescheduling!")
        await broadcast({"type": "state_update"})
        return

    if action == "decline_shield":
        # Target declines to block — apply the attack
        pending = gs.get("pending_shield")
        if not pending or pending["target"] != my_name:
            return
        target_player = next(
            (p for p in gs["players"] if p["name"] == my_name), None)
        attacker_name = pending["attacker"]
        card = pending["card"]
        gs["pending_shield"] = None

        if card["name"] == "Flaked Last Minute":
            desk = target_player["desk"]
            good_cleared = desk["good"]
            desk["good"] = 0
            desk["status"] = "Empty"
            gs["log"].append(
                f"💣 {attacker_name}'s nuke hit {my_name}'s desk! {good_cleared} Good card(s) swept away.")
        elif card["type"] == "Bad":
            desk = target_player["desk"]
            if desk["status"] == "Locked":
                gs["log"].append(
                    f"🔒 {my_name}'s desk is Locked — Bad card bounced!")
            elif desk["good"] > 0:
                desk["good"] -= 1
                if desk["good"] == 0:
                    desk["status"] = "Empty"
                gs["log"].append(
                    f"❌ {attacker_name} sabotaged {my_name}'s desk! ({desk['good']}/3)")
            else:
                gs["log"].append(
                    f"💨 {attacker_name}'s Bad card hit an empty desk — no effect.")
        await broadcast({"type": "state_update"})
        return

    cur_player = gs["players"][gs["current_turn"]]

    # ── DRAW ──────────────────────────────────────────────────────────────────
    if action == "draw":
        if cur_player["name"] != my_name:
            return
        if cur_player["has_drawn"]:
            await send_to(ws, {"type": "error", "msg": "Already drew this turn"})
            return
        for _ in range(2):
            c = draw_card(gs)
            if c:
                cur_player["hand"].append(c)
        cur_player["has_drawn"] = True
        gs["log"].append(f"📦 {my_name} drew 2 cards.")
        await broadcast({"type": "state_update"})
        return

    # ── PLAY CARD ─────────────────────────────────────────────────────────────
    if action == "play_card":
        if cur_player["name"] != my_name:
            return
        if not cur_player["has_drawn"]:
            await send_to(ws, {"type": "error", "msg": "Draw first!"})
            return
        if cur_player["plays_left"] <= 0:
            await send_to(ws, {"type": "error", "msg": "No plays left"})
            return
        card_idx = msg.get("card_idx")
        target_name = msg.get("target_name")   # for Bad / Nuke / Drama
        hand = cur_player["hand"]
        if card_idx is None or not (0 <= card_idx < len(hand)):
            return
        card = hand[card_idx]
        played = False

        # ── Good card → build your own desk ──────────────────────────────────
        if card["type"] == "Good":
            desk = cur_player["desk"]
            if desk["status"] == "Locked":
                # Auto-clear locked desk so player can start a new project
                desk["good"] = 0
                desk["status"] = "Empty"
            desk["good"] += 1
            desk["status"] = "Active"
            gs["log"].append(
                f"✅ {my_name} built on their desk. ({desk['good']}/3)")
            played = True

        # ── The Networker → +2 on your own desk ──────────────────────────────
        elif card["name"] == "The Networker":
            desk = cur_player["desk"]
            if desk["status"] == "Locked":
                desk["good"] = 0
                desk["status"] = "Empty"
            desk["good"] += 2
            desk["status"] = "Active"
            gs["log"].append(
                f"🌐 {my_name} used The Networker! Desk: {desk['good']}/3")
            played = True

        # ── Miracle Rescheduling → on your turn: clear a sabotage on own desk ─
        elif card["name"] == "Miracle Rescheduling":
            desk = cur_player["desk"]
            if desk["good"] == 0:
                await send_to(ws, {"type": "error", "msg": "Nothing to rescue — desk is already empty!"})
                return
            gs["log"].append(
                f"✨ {my_name} used Miracle Rescheduling — desk protected, sabotage cleared!")
            # Treat as undo of last sabotage — restore 1 good card (capped at 2 since 3 would auto-lock)
            desk["good"] = min(desk["good"] + 1, 2)
            desk["status"] = "Active"
            played = True

        # ── Bad card → attack an opponent's desk ─────────────────────────────
        elif card["type"] == "Bad":
            target_p = next(
                (p for p in gs["players"] if p["name"] == target_name), None)
            if not target_p or target_name == my_name:
                await send_to(ws, {"type": "error", "msg": "Pick an opponent to sabotage"})
                return
            if target_p["desk"]["status"] == "Locked":
                await send_to(ws, {"type": "error", "msg": "Locked desks cannot be sabotaged!"})
                return
            used = hand.pop(card_idx)
            use_card(gs, used)
            cur_player["plays_left"] -= 1
            cur_player["cards_played"] += 1
            # Only offer shield if target actually has a Miracle Rescheduling card
            target_has_shield = any(
                c["name"] == "Miracle Rescheduling" for c in target_p["hand"])
            if target_has_shield:
                gs["pending_shield"] = {
                    "attacker": my_name, "target": target_name, "card": card}
                gs["log"].append(
                    f"⚔️ {my_name} is attacking {target_name}'s desk! {target_name} can block with Miracle Rescheduling…")
                await broadcast({"type": "state_update"})
                target_ws = next((w for w, i in clients.items()
                                 if i["name"] == target_name), None)
                if target_ws:
                    await target_ws.send(json.dumps({"type": "shield_opportunity", "attacker": my_name}))
            else:
                # No shield card — apply attack immediately
                desk = target_p["desk"]
                if desk["good"] > 0:
                    desk["good"] -= 1
                    if desk["good"] == 0:
                        desk["status"] = "Empty"
                    gs["log"].append(
                        f"❌ {my_name} sabotaged {target_name}'s desk! ({desk['good']}/3)")
                else:
                    gs["log"].append(
                        f"💨 {my_name}'s Bad card hit {target_name}'s empty desk — no effect.")
                await broadcast({"type": "state_update"})
            return

        # ── Flaked Last Minute → nuke opponent's entire desk ─────────────────
        elif card["name"] == "Flaked Last Minute":
            target_p = next(
                (p for p in gs["players"] if p["name"] == target_name), None)
            if not target_p or target_name == my_name:
                await send_to(ws, {"type": "error", "msg": "Pick an opponent to nuke"})
                return
            if target_p["desk"]["status"] == "Locked":
                await send_to(ws, {"type": "error", "msg": "Locked desks cannot be nuked!"})
                return
            used = hand.pop(card_idx)
            use_card(gs, used)
            cur_player["plays_left"] -= 1
            cur_player["cards_played"] += 1
            target_has_shield = any(
                c["name"] == "Miracle Rescheduling" for c in target_p["hand"])
            if target_has_shield:
                gs["pending_shield"] = {
                    "attacker": my_name, "target": target_name, "card": card}
                gs["log"].append(
                    f"💣 {my_name} launched a NUKE at {target_name}! {target_name} can block…")
                await broadcast({"type": "state_update"})
                target_ws = next((w for w, i in clients.items()
                                 if i["name"] == target_name), None)
                if target_ws:
                    await target_ws.send(json.dumps({"type": "shield_opportunity", "attacker": my_name, "is_nuke": True}))
            else:
                # No shield — nuke lands immediately
                desk = target_p["desk"]
                good_cleared = desk["good"]
                desk["good"] = 0
                desk["status"] = "Empty"
                gs["log"].append(
                    f"💣 {my_name} NUKED {target_name}'s desk! {good_cleared} Good card(s) swept away. (No shield available)")
                await broadcast({"type": "state_update"})
            return

        # ── Group Chat Drama → force opponent to discard + redraw ─────────────
        elif card["name"] == "Group Chat Drama":
            target_p = next(
                (p for p in gs["players"] if p["name"] == target_name), None)
            if not target_p:
                await send_to(ws, {"type": "error", "msg": "Pick a player for Group Chat Drama"})
                return
            lost = len(target_p["hand"])
            for _dc in target_p["hand"]:
                discard_card(gs, _dc)
            target_p["hand"] = []
            for _ in range(lost):
                c = draw_card(gs)
                if c:
                    target_p["hand"].append(c)
            gs["log"].append(
                f"🔥 {my_name} used Group Chat Drama on {target_name}! They redrew {lost} cards.")
            played = True

        # ── Coffee Run → draw 3 extra ─────────────────────────────────────────
        elif card["name"] == "Coffee Run":
            for _ in range(3):
                c = draw_card(gs)
                if c:
                    cur_player["hand"].append(c)
            gs["log"].append(f"☕ {my_name} used Coffee Run! Drew 3 cards.")
            played = True

        # ── Dean's Office ─────────────────────────────────────────────────────
        elif card["name"] == "Dean's Office":
            top5 = []
            for _ in range(min(5, len(gs["deck"]))):
                top5.append(gs["deck"].pop())
            gs["pending_dean"] = {"cards": top5, "player": my_name}
            used = hand.pop(card_idx)
            use_card(gs, used)
            cur_player["plays_left"] -= 1
            cur_player["cards_played"] += 1
            gs["log"].append(f"🏛️ {my_name} visited Dean's Office.")
            await broadcast({"type": "state_update"})
            await ws.send(json.dumps({"type": "dean_cards", "cards": top5}))
            return

        if played:
            used = hand.pop(card_idx)
            use_card(gs, used)
            cur_player["plays_left"] -= 1
            cur_player["cards_played"] += 1
            # Check if desk now locks
            if check_desk_lock(gs, cur_player):
                # clear for next project
                cur_player["desk"] = {"good": 0, "status": "Empty"}
            if check_win(gs):
                await broadcast({"type": "state_update"})
                return
        await broadcast({"type": "state_update"})
        return

    # ── DEAN CHOICE ───────────────────────────────────────────────────────────
    if action == "dean_choice":
        if not gs.get("pending_dean") or gs["pending_dean"]["player"] != my_name:
            return
        keep_idx = msg.get("keep_idx", 0)
        cards = gs["pending_dean"]["cards"]
        cur_player = next(
            (p for p in gs["players"] if p["name"] == my_name), None)
        if not cur_player:
            return
        cur_player["hand"].append(cards.pop(keep_idx))
        gs["deck"] = cards + gs["deck"]
        gs["pending_dean"] = None
        gs["log"].append(f"🏛️ {my_name} kept a card from Dean's Office.")
        await broadcast({"type": "state_update"})
        return

    # ── END TURN ──────────────────────────────────────────────────────────────
    if action == "end_turn":
        cur_player = gs["players"][gs["current_turn"]]
        if cur_player["name"] != my_name:
            return
        if not cur_player["has_drawn"]:
            await send_to(ws, {"type": "error", "msg": "Draw first!"})
            return
        if cur_player.get("cards_played", 0) < 1:
            await send_to(ws, {"type": "error", "msg": "Play at least 1 card before ending your turn!"})
            return
        # Discard to 5
        while len(cur_player["hand"]) > 5:
            discard_card(gs, cur_player["hand"].pop())
        cur_player["plays_left"] = 2
        cur_player["has_drawn"] = False
        cur_player["cards_played"] = 0
        gs["current_turn"] = (gs["current_turn"] + 1) % len(gs["players"])
        gs["log"].append(
            f"➡️ {gs['players'][gs['current_turn']]['name']}'s turn.")
        await broadcast({"type": "state_update"})
        return

    # ── DISCARD CARD (over hand limit) ────────────────────────────────────────
    if action == "discard_card":
        cur_player = gs["players"][gs["current_turn"]]
        if cur_player["name"] != my_name:
            return
        card_idx = msg.get("card_idx")
        if card_idx is None or not (0 <= card_idx < len(cur_player["hand"])):
            return
        if len(cur_player["hand"]) <= 5:
            await send_to(ws, {"type": "error", "msg": "Hand is 5 or fewer already"})
            return
        discard_card(gs, cur_player["hand"].pop(card_idx))
        await broadcast({"type": "state_update"})
        return


# ── HTTP + WebSocket server ───────────────────────────────────────────────────
HTML_FILE = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "nsu_hangout.html")


def read_html():
    try:
        with open(HTML_FILE, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b"<h1>Put nsu_hangout.html in the same folder as server.py</h1>"


async def http_handler(request):
    return web.Response(body=read_html(), content_type="text/html", charset="utf-8")


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Wrap aiohttp WebSocket in an adapter compatible with our handler
    class WSAdapter:
        def __init__(self, aio_ws):
            self._ws = aio_ws

        async def send(self, text):
            await self._ws.send_str(text)

        def __hash__(self):
            return id(self._ws)

        def __eq__(self, other):
            return self is other

    adapted = WSAdapter(ws)
    # Note: client is registered in clients{} only after a successful "join" action

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await handle_message(adapted, msg.data)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        if adapted in clients:
            del clients[adapted]
            await broadcast_lobby()
            if game_state:
                await broadcast({"type": "state_update"})

    return ws


async def main():
    port = int(os.environ.get("PORT", 12350))
    app = web.Application()
    app.router.add_get("/", http_handler)
    app.router.add_get("/ws", ws_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print("=" * 50)
    print("NSU Hustle & Sabotage — server running!")
    print(f"\nOpen in browser: http://localhost:{port}")
    print("=" * 50)
    await asyncio.Future()


if __name__ == "__main__":
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
