#!/usr/bin/env python3
"""
road_rash_cli_full.py

Terminal-playable Road Rash mini-game with:
- Opponent selection menu (Aggressive / Balanced / Random)
- Neutral bikers (random agents) on the track
- Improved attack UI: colored flashes, ASCII attack animation, sound
- Boost (B), hazards, attack cooldowns, health bars
- Final stats shown at game over

Run:
  pip install windows-curses   # on Windows only
  python road_rash_cli_full.py
"""

import curses
import time
import random
import argparse
from collections import deque
from itertools import cycle

# --- Game constants ---
LANES = 3
TRACK_LENGTH = 220.0
VIEWPORT_ROWS = 16
TICK = 0.14
MAX_SPEED = 14.0
ACCEL = 3.0
BRAKE = -5.0
ATTACK_RANGE = 3.5
ATTACK_DAMAGE = 0.28
MAX_HEALTH = 1.0
ATTACK_COOLDOWN_TICKS = int(1.8 / TICK)
BOOST_DURATION_TICKS = int(2.0 / TICK)
BOOST_SPEED_MULT = 1.5
DEPTH_AGGRESSIVE = 4
DEPTH_BALANCED = 3
ACTIONS = ["ACCEL", "BRAKE", "LEFT", "RIGHT", "ATTACK", "MAINTAIN"]

# Hazards / neutrals
HAZARD_COUNT = 8
HAZARD_SLIP_FACTOR = 0.55
HAZARD_DAMAGE = 0.05

NEUTRAL_COUNT = 5
NEUTRAL_MAX_SPEED = 8.0

# Colors (will be init in curses)
COLOR_HIT = 1
COLOR_OUCH = 2
COLOR_CRIT = 3
COLOR_TITLE = 4
COLOR_HAZARD = 5
COLOR_NEUTRAL = 6

# --- Utilities ---
def clamp(v, a, b): return max(a, min(b, v))

def try_beep():
    try:
        import winsound
        winsound.Beep(800, 70)
    except Exception:
        try:
            curses.beep()
        except Exception:
            # Fallback to terminal bell
            print('\a', end='', flush=True)

# --- State ---
class State:
    __slots__ = (
        "p_pos","p_speed","p_lane","p_health",
        "o_pos","o_speed","o_lane","o_health",
        "tick",
        "p_last_attack_tick","o_last_attack_tick",
        "p_boost_avail","p_boost_ticks_left",
        "hazards","neutrals",
        # transient flags
        "_last_hit_by_player","_last_hit_by_opponent",
        "_last_hazard_hit_player","_last_hazard_hit_opponent",
        "_attack_animation"  # tuple (who, from_lane, to_lane, frames_left)
    )

    def __init__(self, p_pos=0.0, p_speed=3.0, p_lane=1, p_health=MAX_HEALTH,
                 o_pos=6.0, o_speed=3.5, o_lane=1, o_health=0.8,
                 tick=0,
                 p_last_attack_tick=-999, o_last_attack_tick=-999,
                 p_boost_avail=True, p_boost_ticks_left=0,
                 hazards=None, neutrals=None):
        self.p_pos = p_pos
        self.p_speed = p_speed
        self.p_lane = p_lane
        self.p_health = p_health
        self.o_pos = o_pos
        self.o_speed = o_speed
        self.o_lane = o_lane
        self.o_health = o_health
        self.tick = tick
        self.p_last_attack_tick = p_last_attack_tick
        self.o_last_attack_tick = o_last_attack_tick
        self.p_boost_avail = p_boost_avail
        self.p_boost_ticks_left = p_boost_ticks_left
        self.hazards = hazards if hazards is not None else []
        self.neutrals = neutrals if neutrals is not None else []

        # transient UI flags
        self._last_hit_by_player = False
        self._last_hit_by_opponent = False
        self._last_hazard_hit_player = False
        self._last_hazard_hit_opponent = False
        self._attack_animation = None

    def copy(self):
        s = State(
            self.p_pos, self.p_speed, self.p_lane, self.p_health,
            self.o_pos, self.o_speed, self.o_lane, self.o_health,
            self.tick,
            self.p_last_attack_tick, self.o_last_attack_tick,
            self.p_boost_avail, self.p_boost_ticks_left,
            list(self.hazards),
            [n.copy() for n in self.neutrals]
        )
        s._last_hit_by_player = self._last_hit_by_player
        s._last_hit_by_opponent = self._last_hit_by_opponent
        s._last_hazard_hit_player = self._last_hazard_hit_player
        s._last_hazard_hit_opponent = self._last_hazard_hit_opponent
        s._attack_animation = self._attack_animation
        return s

# Neutral biker as tiny class
class Neutral:
    __slots__ = ("pos","lane","speed","health","id","tick_last_dir")
    def __init__(self, pos, lane, speed, health, id):
        self.pos = pos
        self.lane = lane
        self.speed = speed
        self.health = health
        self.id = id
        self.tick_last_dir = 0

    def copy(self):
        n = Neutral(self.pos, self.lane, self.speed, self.health, self.id)
        n.tick_last_dir = self.tick_last_dir
        return n

# --- World helpers ---
def spawn_hazards():
    hazards = []
    for _ in range(HAZARD_COUNT):
        pos = random.uniform(20.0, TRACK_LENGTH - 20.0)
        lane = random.randrange(0, LANES)
        typ = random.choice(["pothole", "oil"])
        hazards.append((pos, lane, typ))
    return hazards

def spawn_neutrals():
    neutrals = []
    for i in range(NEUTRAL_COUNT):
        pos = random.uniform(10.0, TRACK_LENGTH - 30.0)
        lane = random.randrange(0, LANES)
        speed = random.uniform(2.0, NEUTRAL_MAX_SPEED)
        health = 0.6 + random.random() * 0.4
        neutrals.append(Neutral(pos, lane, speed, health, i+1))
    return neutrals

# --- Simulation ---
def simulate_one_tick(state: State, pa: str, oa: str, dt=TICK) -> State:
    s = state.copy()

    # apply base speed change
    def apply_speed(speed, act, is_player=False):
        if act == "ACCEL":
            speed += ACCEL * dt
        elif act == "BRAKE":
            speed += BRAKE * dt
        # clamp with boost if player boost active
        if is_player and s.p_boost_ticks_left > 0:
            speed = clamp(speed, 0.0, MAX_SPEED * BOOST_SPEED_MULT)
        else:
            speed = clamp(speed, 0.0, MAX_SPEED)
        return speed

    s.p_speed = apply_speed(s.p_speed, pa, is_player=True)
    s.o_speed = apply_speed(s.o_speed, oa, is_player=False)

    # neutrals update: random lane drift and small accel/brake
    for n in s.neutrals:
        # random lane change occasionally
        if s.tick - n.tick_last_dir > int(0.6 / TICK) and random.random() < 0.25:
            n.lane = clamp(n.lane + random.choice([-1,0,1]), 0, LANES-1)
            n.tick_last_dir = s.tick
        # small random accel
        n.speed += random.uniform(-0.6, 0.6) * dt
        n.speed = clamp(n.speed, 0.5, NEUTRAL_MAX_SPEED)
        # update position
        n.pos += n.speed * dt

    # lane changes for player/opponent
    if pa == "LEFT": s.p_lane = clamp(s.p_lane - 1, 0, LANES-1)
    if pa == "RIGHT": s.p_lane = clamp(s.p_lane + 1, 0, LANES-1)
    if oa == "LEFT": s.o_lane = clamp(s.o_lane - 1, 0, LANES-1)
    if oa == "RIGHT": s.o_lane = clamp(s.o_lane + 1, 0, LANES-1)

    # update positions
    s.p_pos += s.p_speed * dt
    s.o_pos += s.o_speed * dt

    # handle boost ticks
    if s.p_boost_ticks_left > 0:
        s.p_boost_ticks_left -= 1
        if s.p_boost_ticks_left == 0:
            s.p_speed = clamp(s.p_speed, 0.0, MAX_SPEED)

    # reset transient flags
    s._last_hit_by_player = False
    s._last_hit_by_opponent = False
    s._last_hazard_hit_player = False
    s._last_hazard_hit_opponent = False
    s._attack_animation = None

    # attacks (with cooldown)
    if pa == "ATTACK" and (s.tick - s.p_last_attack_tick) >= ATTACK_COOLDOWN_TICKS:
        s.p_last_attack_tick = s.tick
        # priority: check opponent, then neutrals
        if s.p_lane == s.o_lane and abs(s.p_pos - s.o_pos) <= ATTACK_RANGE:
            s.o_health = clamp(s.o_health - ATTACK_DAMAGE, 0.0, MAX_HEALTH)
            s._last_hit_by_player = True
            s._attack_animation = ("P", s.p_lane, s.o_lane, 4)
            try_beep()
        else:
            # check neutrals
            for n in s.neutrals:
                if n.health > 0 and n.lane == s.p_lane and abs(n.pos - s.p_pos) <= ATTACK_RANGE:
                    n.health = clamp(n.health - ATTACK_DAMAGE, 0.0, MAX_HEALTH)
                    s._last_hit_by_player = True
                    s._attack_animation = ("P", s.p_lane, n.lane, 4)
                    try_beep()
                    break

    if oa == "ATTACK" and (s.tick - s.o_last_attack_tick) >= ATTACK_COOLDOWN_TICKS:
        s.o_last_attack_tick = s.tick
        # opponent attack can hit player or neutrals
        if s.o_lane == s.p_lane and abs(s.o_pos - s.p_pos) <= ATTACK_RANGE:
            s.p_health = clamp(s.p_health - ATTACK_DAMAGE, 0.0, MAX_HEALTH)
            s._last_hit_by_opponent = True
            s._attack_animation = ("O", s.o_lane, s.p_lane, 4)
            try_beep()
        else:
            for n in s.neutrals:
                if n.health > 0 and n.lane == s.o_lane and abs(n.pos - s.o_pos) <= ATTACK_RANGE:
                    n.health = clamp(n.health - ATTACK_DAMAGE, 0.0, MAX_HEALTH)
                    s._last_hit_by_opponent = True
                    s._attack_animation = ("O", s.o_lane, n.lane, 4)
                    try_beep()
                    break

    # hazards: neutrals/player/opponent hitting hazards
    new_hz = []
    for hz in s.hazards:
        hz_pos, hz_lane, hz_type = hz
        # player
        if hz_lane == s.p_lane and abs(s.p_pos - hz_pos) < (s.p_speed * dt + 0.6):
            s.p_speed *= HAZARD_SLIP_FACTOR
            s.p_health = clamp(s.p_health - HAZARD_DAMAGE, 0.0, MAX_HEALTH)
            s._last_hazard_hit_player = True
        # opponent
        if hz_lane == s.o_lane and abs(s.o_pos - hz_pos) < (s.o_speed * dt + 0.6):
            if random.random() < 0.7:
                s.o_speed *= (HAZARD_SLIP_FACTOR + 0.08 * random.random())
                s.o_health = clamp(s.o_health - (HAZARD_DAMAGE * (0.5 + random.random())), 0.0, MAX_HEALTH)
                s._last_hazard_hit_opponent = True
        # neutrals
        for n in s.neutrals:
            if hz_lane == n.lane and abs(n.pos - hz_pos) < (n.speed * dt + 0.6):
                if random.random() < 0.6:
                    n.speed *= (HAZARD_SLIP_FACTOR + 0.1 * random.random())
                    n.health = clamp(n.health - (HAZARD_DAMAGE * (0.5 + random.random())), 0.0, MAX_HEALTH)
        new_hz.append(hz)
    s.hazards = new_hz

    # simple neutral collisions with player/opponent causing small bump damage
    for n in s.neutrals:
        if n.health <= 0: continue
        # neutral hitting player
        if n.lane == s.p_lane and abs(n.pos - s.p_pos) < 0.8:
            # small bump damage and knockback (speed reduction)
            s.p_health = clamp(s.p_health - 0.02, 0.0, MAX_HEALTH)
            s.p_speed *= 0.9
        # neutral hitting opponent
        if n.lane == s.o_lane and abs(n.pos - s.o_pos) < 0.8:
            s.o_health = clamp(s.o_health - 0.02, 0.0, MAX_HEALTH)
            s.o_speed *= 0.9

    s.tick += 1
    return s

# --- Terminal check ---
def is_terminal(s: State) -> bool:
    if s.p_health <= 0 or s.o_health <= 0:
        return True
    if s.p_pos >= TRACK_LENGTH or s.o_pos >= TRACK_LENGTH:
        return True
    if s.tick > 7000:
        return True
    return False

# --- Evaluation (player-centric) ---
def evaluate_state_fun(s: State, mode="balanced") -> float:
    progress = s.p_pos / TRACK_LENGTH
    lead = (s.p_pos - s.o_pos) / TRACK_LENGTH
    speed_norm = s.p_speed / (MAX_SPEED * BOOST_SPEED_MULT)
    health = s.p_health
    collision = 1.0 if (s.p_lane == s.o_lane and abs(s.p_pos - s.o_pos) < 3.0) else 0.0
    attack_op = 1.0 if (s.p_lane == s.o_lane and 0 < (s.p_pos - s.o_pos) <= ATTACK_RANGE) else 0.0

    if mode == "aggressive":
        w_progress, w_speed, w_lead, w_health, w_collision, w_attack = 55, 18, 35, 60, -50, 70
    else:  # balanced
        w_progress, w_speed, w_lead, w_health, w_collision, w_attack = 60, 18, 40, 80, -65, 45

    score = (w_progress * progress +
             w_speed * speed_norm +
             w_lead * lead +
             w_health * health +
             w_collision * collision +
             w_attack * attack_op)
    return score

# --- Opponent AI ---
def opponent_choose_action(state: State, opponent_type="balanced") -> str:
    # opponent_type: "aggressive", "balanced", "random"
    if opponent_type == "random":
        # random action but with some heuristics
        if state.o_lane == state.p_lane and abs(state.o_pos - state.p_pos) <= ATTACK_RANGE + 0.7:
            # try attack sometimes
            return "ATTACK" if random.random() < 0.5 else random.choice(ACTIONS)
        return random.choice(ACTIONS)

    depth = DEPTH_BALANCED if opponent_type == "balanced" else DEPTH_AGGRESSIVE
    alpha = -1e9
    beta = 1e9
    best_action = "MAINTAIN"
    best_val = 1e9
    candidate_actions = ACTIONS[:]
    random.shuffle(candidate_actions)
    # prefer attack if in range
    if state.o_lane == state.p_lane and abs(state.o_pos - state.p_pos) <= ATTACK_RANGE + 0.5:
        candidate_actions.sort(key=lambda a: 0 if a == "ATTACK" else 1)

    for a in candidate_actions:
        child = simulate_one_tick(state, "MAINTAIN", a)
        # After opponent acts, we let the recursive search evaluate from player's perspective.
        val = _alphabeta(child, depth-1, alpha, beta, maximizing=True, mode=opponent_type)
        if val < best_val:
            best_val = val
            best_action = a
        beta = min(beta, best_val)
        if alpha >= beta:
            break
    # add small randomness for human fun
    if random.random() < 0.1:
        return random.choice(ACTIONS)
    return best_action

def _alphabeta(s: State, depth: int, alpha: float, beta: float, maximizing: bool, mode="balanced") -> float:
    if depth == 0 or is_terminal(s):
        return evaluate_state_fun(s, mode)
    if maximizing:
        v = -1e9
        for a in ACTIONS:
            child = simulate_one_tick(s, a, "MAINTAIN")
            v = max(v, _alphabeta(child, depth-1, alpha, beta, False, mode))
            alpha = max(alpha, v)
            if alpha >= beta:
                break
        return v
    else:
        v = 1e9
        for a in ACTIONS:
            child = simulate_one_tick(s, "MAINTAIN", a)
            v = min(v, _alphabeta(child, depth-1, alpha, beta, True, mode))
            beta = min(beta, v)
            if alpha >= beta:
                break
        return v

# --- Input map ---
KEY_MAP = {
    ord('w'): "ACCEL",
    ord('s'): "BRAKE",
    ord('a'): "LEFT",
    ord('d'): "RIGHT",
    ord('k'): "ATTACK",
    ord(' '): "MAINTAIN",
    ord('b'): "BOOST",
    ord('B'): "BOOST",
}

# --- Rendering helpers ---
def health_bar(prefix, val, width=22):
    pct = clamp(val, 0.0, 1.0)
    filled = int(round(pct * width))
    bar = "[" + "█" * filled + " " * (width - filled) + "]"
    return f"{prefix}: {bar} {int(pct*100)}%"

def draw_attack_anim(stdscr, s: State, center_x):
    if not s._attack_animation:
        return
    who, from_lane, to_lane, frames_left = s._attack_animation
    # show a small '->' across the middle lane area, just a decorative pulse
    anim_row = 8  # near viewport top
    anim_str = "  -->  "
    try:
        stdscr.addstr(anim_row, center_x - len(anim_str)//2, anim_str, curses.A_BOLD)
    except Exception:
        # if drawing fails due to size, ignore
        pass

def draw_game(stdscr, s: State, last_actions, flash_message=None):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    center_x = w // 2
    # Title
    title = "Road Rash CLI — W A S D K=attack  B=boost  R=restart  Q=quit"
    try:
        stdscr.attron(curses.color_pair(COLOR_TITLE))
        stdscr.addstr(0, max(0, center_x - len(title)//2), title)
        stdscr.attroff(curses.color_pair(COLOR_TITLE))
    except Exception:
        # Ignore draw errors on small terminals
        pass

    # status
    status = f"Tick:{s.tick}  You: pos={s.p_pos:.1f} sp={s.p_speed:.1f} hp={s.p_health:.2f} lane={s.p_lane}  " \
             f"Opp: pos={s.o_pos:.1f} sp={s.o_speed:.1f} hp={s.o_health:.2f} lane={s.o_lane}"
    try:
        stdscr.addstr(2, 2, status)
    except Exception:
        pass

    # health bars
    try:
        stdscr.addstr(3, 2, health_bar("Player   ", s.p_health))
        stdscr.addstr(4, 2, health_bar("Opponent ", s.o_health))
    except Exception:
        pass

    # neutrals health summary small
    alive_neutrals = sum(1 for n in s.neutrals if n.health > 0)
    try:
        stdscr.addstr(5, 2, f"Neutrals alive: {alive_neutrals}/{len(s.neutrals)}")
    except Exception:
        pass

    # boost info
    boost_info = f"Boost: {'READY' if s.p_boost_avail else ('ACTIVE' if s.p_boost_ticks_left>0 else 'USED')}"
    if s.p_boost_ticks_left > 0:
        boost_info += f" ({s.p_boost_ticks_left} ticks)"
    try:
        stdscr.addstr(3, w - len(boost_info) - 4, boost_info)
    except Exception:
        pass

    # last actions
    la = f"Last actions -> You: {last_actions.get('you','-'):7}  Opp: {last_actions.get('opp','-'):7}"
    try:
        stdscr.addstr(6, 2, la)
    except Exception:
        pass

    # flash message with color
    if flash_message:
        style = curses.A_REVERSE
        try:
            if flash_message.startswith("YOU HIT"):
                style |= curses.color_pair(COLOR_HIT)
            elif flash_message.startswith("YOU GOT HIT"):
                style |= curses.color_pair(COLOR_OUCH)
            elif flash_message.startswith("CRIT") or flash_message.startswith("GAME OVER"):
                style |= curses.color_pair(COLOR_CRIT)
        except Exception:
            pass
        try:
            stdscr.addstr(7, max(0, center_x - len(flash_message)//2), flash_message, style)
        except Exception:
            pass

    # viewport
    start_row = 9
    for row in range(VIEWPORT_ROWS):
        rel = (VIEWPORT_ROWS - 1 - row) * (MAX_SPEED * TICK * 3)
        row_pos = s.p_pos + rel
        row_str = ""
        for lane in range(LANES):
            cell = "   "
            # hazards
            for hz in s.hazards:
                hz_pos, hz_lane, hz_type = hz
                if lane == hz_lane and int(round(hz_pos)) == int(round(row_pos)):
                    cell = " ~ "
            # neutrals
            for n in s.neutrals:
                if n.health > 0 and int(round(n.pos)) == int(round(row_pos)) and n.lane == lane:
                    cell = " n "
            # player/opponent
            if int(round(s.p_pos)) == int(round(row_pos)) and s.p_lane == lane:
                cell = " P "
            if int(round(s.o_pos)) == int(round(row_pos)) and s.o_lane == lane:
                if int(round(s.p_pos)) == int(round(row_pos)) and s.p_lane == lane:
                    cell = " X "
                else:
                    cell = " O "
            row_str += "|" + cell
        row_str += "|"
        try:
            stdscr.addstr(start_row + row, max(0, center_x - len(row_str)//2), row_str)
        except Exception:
            # ignore rows that cannot be drawn
            pass

    # attack animation (decorative)
    if s._attack_animation:
        draw_attack_anim(stdscr, s, center_x)

    # footer: progress
    finish_pct = min(100.0, (s.p_pos / TRACK_LENGTH) * 100.0)
    opp_finish_pct = min(100.0, (s.o_pos / TRACK_LENGTH) * 100.0)
    footer = f"Finish: P {finish_pct:.1f}%  O {opp_finish_pct:.1f}%"
    try:
        stdscr.addstr(start_row + VIEWPORT_ROWS + 1, max(0, center_x - len(footer)//2), footer)
    except Exception:
        pass

    stdscr.refresh()

# --- Game loop ---
def game_loop(stdscr, opponent_type="balanced"):
    # init
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(0)

    # init colors
    curses.start_color()
    try:
        curses.init_pair(COLOR_HIT, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(COLOR_OUCH, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(COLOR_CRIT, curses.COLOR_RED, curses.COLOR_BLACK)
        curses.init_pair(COLOR_TITLE, curses.COLOR_CYAN, curses.COLOR_BLACK)
        curses.init_pair(COLOR_HAZARD, curses.COLOR_MAGENTA, curses.COLOR_BLACK)
        curses.init_pair(COLOR_NEUTRAL, curses.COLOR_BLUE, curses.COLOR_BLACK)
    except Exception:
        # If terminal doesn't support colors, ignore
        pass

    hazards = spawn_hazards()
    neutrals = spawn_neutrals()

    s = State(
        p_pos=0.0, p_speed=3.0, p_lane=1, p_health=MAX_HEALTH,
        o_pos=6.0, o_speed=3.5, o_lane=1, o_health=0.8,
        tick=0,
        p_last_attack_tick=-999, o_last_attack_tick=-999,
        p_boost_avail=True, p_boost_ticks_left=0,
        hazards=hazards, neutrals=neutrals
    )

    last_time = time.time()
    key_buffer = deque(maxlen=4)
    last_actions = {"you":"-", "opp":"-"}
    flash_until = 0
    flash_message = None

    while True:
        now = time.time()
        if now - last_time >= TICK:
            # opponent action
            oa = opponent_choose_action(s, opponent_type)
            # player action
            pa = "MAINTAIN"
            if key_buffer:
                k = key_buffer.pop()
                mapped = KEY_MAP.get(k, None)
                if mapped == "BOOST":
                    if s.p_boost_avail:
                        s.p_boost_avail = False
                        s.p_boost_ticks_left = BOOST_DURATION_TICKS
                else:
                    pa = mapped if mapped else "MAINTAIN"

            # advance world
            s = simulate_one_tick(s, pa, oa, dt=TICK)

            # last actions
            last_actions["you"] = pa
            last_actions["opp"] = oa

            # flash messages & sound
            fm = None
            if s._last_hit_by_player:
                fm = "YOU HIT! +damage"
                try_beep()
                flash_until = s.tick + int(0.8 / TICK)
            if s._last_hit_by_opponent:
                fm = "YOU GOT HIT! -damage"
                try_beep()
                flash_until = s.tick + int(0.8 / TICK)
            if s._last_hazard_hit_player:
                fm = "You hit a HAZARD!"
                flash_until = s.tick + int(0.9 / TICK)
            if s._last_hazard_hit_opponent:
                fm = "Opponent hit a HAZARD!"
                flash_until = s.tick + int(0.9 / TICK)

            # critical warning
            if s.p_health < 0.25:
                fm = "CRIT: Low Health!"
                flash_until = s.tick + int(0.8 / TICK)

            if s.tick <= flash_until:
                flash_message = fm
            else:
                flash_message = None

            draw_game(stdscr, s, last_actions, flash_message)

            # terminal?
            if is_terminal(s):
                # compute final stats
                alive_neuts = [n for n in s.neutrals if n.health > 0]
                total_neuts = len(s.neutrals)
                winner = "DRAW"
                if s.p_health > 0 and s.o_health <= 0: winner = "PLAYER"
                elif s.o_health > 0 and s.p_health <= 0: winner = "OPPONENT"
                elif s.p_pos >= TRACK_LENGTH and s.o_pos < TRACK_LENGTH: winner = "PLAYER"
                elif s.o_pos >= TRACK_LENGTH and s.p_pos < TRACK_LENGTH: winner = "OPPONENT"
                else:
                    if s.p_pos > s.o_pos: winner = "PLAYER"
                    elif s.o_pos > s.p_pos: winner = "OPPONENT"

                # show final screen
                stdscr.erase()
                center_x = stdscr.getmaxyx()[1]//2
                try:
                    stdscr.addstr(2, max(0, center_x-6), "=== GAME OVER ===", curses.A_BOLD)
                except Exception:
                    pass
                try:
                    stdscr.addstr(4, 4, f"Winner: {winner}")
                    stdscr.addstr(6, 4, f"Your Health:     {s.p_health:.3f}")
                    stdscr.addstr(7, 4, f"Opponent Health: {s.o_health:.3f}")
                    stdscr.addstr(8, 4, f"Neutrals alive:  {len(alive_neuts)}/{total_neuts}")
                    stdscr.addstr(10, 4, "Press R to restart or Q to quit.")
                except Exception:
                    pass
                stdscr.refresh()

                # wait for choice
                while True:
                    ch = stdscr.getch()
                    if ch in (ord('q'), ord('Q')):
                        return
                    if ch in (ord('r'), ord('R')):
                        return game_loop(stdscr, opponent_type)
                    time.sleep(0.05)

            last_time = now

        # handle input
        try:
            ch = stdscr.getch()
        except Exception:
            ch = -1
        if ch != -1:
            if ch in (ord('q'), ord('Q')):
                return
            if ch in (ord('r'), ord('R')):
                return game_loop(stdscr, opponent_type)
            if ch in KEY_MAP:
                key_buffer.append(ch)
        # small sleep to avoid busy-waiting
        time.sleep(0.002)

# --- Splash screen & Menu ---
def splash_screen(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(1)
    stdscr.clear()
    h, w = stdscr.getmaxyx()
    center_x = w // 2

    # ASCII Title
    ASCII_LETTERS = {
        "A": ["  ███  ", " █   █ ", " █████ ", " █   █ ", " █   █ "],
        "D": [" ████  ", " █   █ ", " █   █ ", " █   █ ", " ████  "],
        "H": [" █   █ ", " █   █ ", " █████ ", " █   █ ", " █   █ "],
        "O": [" █████ ", " █   █ ", " █   █ ", " █   █ ", " █████ "],
        "R": [" █████ ", " █   █ ", " █████ ", " █  █  ", " █   █ "],
        "S": [" █████ ", " █     ", " █████ ", "     █ ", " █████ "],
        " ": ["       ", "       ", "       ", "       ", "       "]
    }

    title_text = "ROAD RASH"
    title_lines = [""] * 5
    for char in title_text:
        letter = ASCII_LETTERS.get(char, ASCII_LETTERS[" "])
        for i in range(5):
            title_lines[i] += letter[i] + "  "

    # Animate letters sliding from top
    for offset in range(-5, 3):
        stdscr.clear()
        for i, line in enumerate(title_lines):
            y = i + 2 + offset
            if 0 <= y < h:
                try:
                    stdscr.addstr(y, max(0, center_x - len(line)//2), line, curses.A_BOLD)
                except Exception:
                    pass
        stdscr.refresh()
        time.sleep(0.05)

    # Trophy ASCII Art
    trophy_ascii_art = [
        "        ___________        ",
        "       '._==_==_=_.'       ",
        "       .-\\:      /-.       ",
        "      | (|:.     |) |      ",
        "       '-|:.     |-'       ",
        "         \\::.    /         ",
        "          '::. .'          ",
        "            ) (            ",
        "          _.' '._          ",
        "       __/_______\\__       ",
        "      /             \\     ",
        "      \\_____________/     ",
    ]

    trophy_h = len(trophy_ascii_art)
    trophy_w = max(len(line) for line in trophy_ascii_art)
    start_y = h // 2 - trophy_h // 2

    # Animate trophy sliding in from right
    for x in range(w, (w - trophy_w)//2 - 1, -2):
        stdscr.clear()
        # Draw title
        for i, line in enumerate(title_lines):
            try:
                stdscr.addstr(i + 2, max(0, center_x - len(line)//2), line, curses.A_BOLD)
            except Exception:
                pass
        # Draw trophy
        for i, line in enumerate(trophy_ascii_art):
            if 0 <= start_y + i < h:
                try:
                    stdscr.addstr(start_y + i, x, line)
                except Exception:
                    pass
        stdscr.refresh()
        time.sleep(0.05)

    # Subtitle
    subtitle = "Classic Road Rash CLI Edition"
    try:
        stdscr.addstr(start_y + trophy_h + 2, max(0, center_x - len(subtitle)//2), subtitle, curses.A_DIM)
    except Exception:
        pass

    # Press any key prompt
    prompt = "Press any key to start..."
    waiting = True
    while waiting:
        try:
            stdscr.addstr(h - 3, max(0, center_x - len(prompt)//2), prompt, curses.A_BLINK)
        except Exception:
            pass
        stdscr.refresh()
        key = stdscr.getch()
        if key != -1:
            waiting = False
        else:
            time.sleep(0.05)
    stdscr.nodelay(False)

# --- Menu / Entrypoint ---
def main_menu(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(-1)
    stdscr.clear()
    h, w = stdscr.getmaxyx()
    center_x = w // 2

    # ASCII Motorcycle Art
    ascii_art = [
        r"    __o",
        r"  _ \<,_",
        r" (_)/(_)"
    ]

    # Title and subtitle
    title = "ROAD RASH CLI"
    subtitle = "Choose Opponent AI"

    # Animate motorcycle entering from left
    art_width = max(len(line) for line in ascii_art)
    target_x = center_x - art_width // 2
    for step in range(-art_width, target_x + 1, 2):  # move in steps of 2 cols
        stdscr.erase()

        # Draw title
        try:
            stdscr.attron(curses.A_BOLD | curses.A_UNDERLINE)
            stdscr.addstr(len(ascii_art) + 2, max(0, center_x - len(title)//2), title)
            stdscr.attroff(curses.A_BOLD | curses.A_UNDERLINE)
        except Exception:
            pass

        # Draw subtitle
        try:
            stdscr.addstr(len(ascii_art) + 4, max(0, center_x - len(subtitle)//2), subtitle)
        except Exception:
            pass

        # Draw bike at current step
        for i, line in enumerate(ascii_art):
            try:
                stdscr.addstr(i + 1, max(0, step), line, curses.A_BOLD)
            except Exception:
                pass

        # Footer hint during animation
        footer = "↑/↓ or W/S to move | Enter to select | Q to quit"
        try:
            stdscr.addstr(h - 2, max(0, center_x - len(footer)//2), footer, curses.A_DIM)
        except Exception:
            pass

        stdscr.refresh()
        time.sleep(0.05)

    # Options
    options = ["Aggressive", "Balanced", "Random"]
    selected = 0

    while True:
        for i, opt in enumerate(options):
            x = center_x - len(opt)//2
            y = len(ascii_art) + 6 + i
            try:
                if i == selected:
                    stdscr.attron(curses.A_REVERSE)
                    stdscr.addstr(y, x, opt)
                    stdscr.attroff(curses.A_REVERSE)
                else:
                    stdscr.addstr(y, x, opt)
            except Exception:
                pass

        # Footer
        footer = "↑/↓ or W/S to move | Enter to select | Q to quit"
        try:
            stdscr.addstr(h - 2, max(0, center_x - len(footer)//2), footer, curses.A_DIM)
        except Exception:
            pass

        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord('w'), ord('W')):
            selected = (selected - 1) % len(options)
        elif ch in (curses.KEY_DOWN, ord('s'), ord('S')):
            selected = (selected + 1) % len(options)
        elif ch in (curses.KEY_ENTER, 10, 13):
            return options[selected].lower()
        elif ch in (ord('q'), ord('Q')):
            return None

def main():
    parser = argparse.ArgumentParser(description="Road Rash CLI Full")
    parser.add_argument("--skip-menu", action="store_true",
                        help="Skip menu and use balanced AI")
    args = parser.parse_args()

    def wrapper(stdscr):
        curses.curs_set(0)

        if args.skip_menu:
            opponent_type = "balanced"
        else:
            # Show splash first
            splash_screen(stdscr)
            opponent_type = main_menu(stdscr)
            if opponent_type is None:  # user quit
                return

        game_loop(stdscr, opponent_type)

    curses.wrapper(wrapper)

if __name__ == "__main__":
    main()
