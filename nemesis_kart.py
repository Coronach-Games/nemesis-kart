import random
import time
import threading
import queue
import math

# --- Configuration Panel (Defaults) ---
CONFIG = {
    "track_length": 1000,  # Arbitrary units for track length
    "num_racers": 8,
    "player_controlled": True, # Set to False to have player be AI controlled too
    "base_speed_min": 8,
    "base_speed_max": 12,
    "boost_speed_bonus": 15,
    "boost_duration": 3, # in steps
    "shell_hit_speed_penalty": -20, # Direct speed reduction on hit
    "shell_hit_duration": 2, # Steps stunned/slowed
    "banana_hit_speed_penalty": -15,
    "banana_hit_duration": 3,
    "item_box_spacing": 200, # How far apart item boxes are
    "item_chance_boost": 0.4,
    "item_chance_green_shell": 0.3,
    "item_chance_red_shell": 0.2,
    "item_chance_banana": 0.1,
    "catch_up_assist_threshold": 150, # How far behind to get better items
    "catch_up_item_boost_mult": 1.5, # Multiplier for boost chance when behind
    "nemesis_hit_relationship_penalty": -3,
    "nemesis_overtake_relationship_penalty": -1, # Penalty for the one overtaken
    "nemesis_finish_ahead_penalty": -2, # Penalty for loser towards winner
    "nemesis_targeting_threshold": -5, # How negative relationship needs to be to prioritize target
    "nemesis_trait_threshold": 3, # How many times an event needs to happen for a basic trait
    "simulation_step_delay": 0.3, # Seconds between steps
}

# Ensure item chances sum roughly to 1 (or adjust logic)
_total_chance = CONFIG["item_chance_boost"] + CONFIG["item_chance_green_shell"] + CONFIG["item_chance_red_shell"] + CONFIG["item_chance_banana"]
CONFIG["item_chance_boost"] /= _total_chance
CONFIG["item_chance_green_shell"] /= _total_chance
CONFIG["item_chance_red_shell"] /= _total_chance
CONFIG["item_chance_banana"] /= _total_chance


# --- Item Definitions ---
class Item:
    BOOST = "Boost"
    GREEN_SHELL = "Green Shell"
    RED_SHELL = "Red Shell"
    BANANA = "Banana"

# --- Nemesis Traits ---
class Trait:
    AGGRESSIVE = "Aggressive" # More likely to use offensive items quickly
    SHELL_SHOCKED = "Shell-Shocked" # Briefly slower after *any* hit
    TARGET_FIXATED = "Target Fixated" # Over-prioritizes nemesis target
    SLIPPERY = "Slippery" # Slightly higher chance to dodge bananas? (Maybe too complex for V1)

# --- Racer Class ---
class Racer:
    def __init__(self, name, is_player=False):
        self.name = name
        self.is_player = is_player
        self.position = 0.0
        self.speed = random.uniform(CONFIG["base_speed_min"], CONFIG["base_speed_max"])
        self.base_speed = self.speed
        self.current_item = None
        self.item_uses = {Item.BOOST: 0, Item.GREEN_SHELL: 0, Item.RED_SHELL: 0, Item.BANANA: 0}
        self.times_hit_by = {Item.GREEN_SHELL: 0, Item.RED_SHELL: 0, Item.BANANA: 0}

        # Status Effects
        self.boost_timer = 0
        self.hit_timer = 0
        self.last_hit_by = None # Racer who last hit this one

        # Nemesis Data
        self.relationships = {} # Key: Racer Name, Value: Relationship Score (-10 to +10)
        self.traits = set()
        self.hit_by_count = {} # Key: Attacker Name, Value: Count
        self.hit_others_count = {} # Key: Target Name, Value: Count

        # Internal state for AI / Item Box
        self.next_item_box_pos = CONFIG["item_box_spacing"]

    def initialize_relationships(self, all_racers):
        for r in all_racers:
            if r.name != self.name:
                self.relationships[r.name] = 0
                self.hit_by_count[r.name] = 0
                self.hit_others_count[r.name] = 0

    def update_relationship(self, other_racer_name, change):
        if other_racer_name in self.relationships:
            self.relationships[other_racer_name] = max(-10, min(10, self.relationships[other_racer_name] + change))
            # Debug log maybe? Handled in Game class

    def add_trait(self, trait):
        if trait not in self.traits:
            self.traits.add(trait)
            debug_log(f"Nemesis: {self.name} gained trait: {trait}")

    def check_trait_conditions(self):
         # Aggressive: Used many offensive items
        offensive_uses = self.item_uses[Item.GREEN_SHELL] + self.item_uses[Item.RED_SHELL]
        if offensive_uses >= CONFIG["nemesis_trait_threshold"] and Trait.AGGRESSIVE not in self.traits:
            self.add_trait(Trait.AGGRESSIVE)

        # Shell-Shocked: Got hit by shells often
        shell_hits = self.times_hit_by[Item.GREEN_SHELL] + self.times_hit_by[Item.RED_SHELL]
        if shell_hits >= CONFIG["nemesis_trait_threshold"] and Trait.SHELL_SHOCKED not in self.traits:
             self.add_trait(Trait.SHELL_SHOCKED)

        # Target Fixated: Strong negative relationship exists
        has_strong_negative = any(rel <= CONFIG["nemesis_targeting_threshold"] for rel in self.relationships.values())
        if has_strong_negative and Trait.TARGET_FIXATED not in self.traits:
             self.add_trait(Trait.TARGET_FIXATED)
        elif not has_strong_negative and Trait.TARGET_FIXATED in self.traits:
             self.traits.remove(Trait.TARGET_FIXATED) # Lose trait if no target anymore


    def decide_action(self, game_state):
        if self.is_player:
            # Player action handled by input loop
            return None, None

        # --- AI Decision Logic ---
        if not self.current_item:
            return "drive", None # No item, just drive

        # Basic Nemesis Targeting
        potential_targets = []
        my_pos_index = game_state['positions'].index((self.name, self.position))

        # Find racers ahead or behind based on item
        racers_ahead = [r for r_name, r_pos in game_state['positions'][my_pos_index+1:] if r_pos > self.position]
        racers_behind = [r for r_name, r_pos in game_state['positions'][:my_pos_index] if r_pos < self.position]
        
        target = None
        nemesis_target = None

        # Check for strong negative relationship (Nemesis target)
        strong_negative_targets = {name: r for name, r in game_state['racers'].items() if self.relationships.get(name, 0) <= CONFIG["nemesis_targeting_threshold"]}

        if strong_negative_targets:
             # Simplistic: pick the 'most hated' one that's in range if possible
             most_hated_name = min(strong_negative_targets, key=lambda name: self.relationships[name])
             nemesis_target = game_state['racers'][most_hated_name]


        # Item specific logic
        if self.current_item == Item.BOOST:
            # Use boost if not already boosting and maybe save for a straight? (simple: use immediately)
            if self.boost_timer <= 0:
                return "use_item", None
        elif self.current_item == Item.GREEN_SHELL:
             # Use if someone is roughly directly ahead or behind (simple: fire forward if anyone ahead)
             # Nemesis: Prioritize hitting nemesis if ahead?
             if nemesis_target and nemesis_target in racers_ahead:
                 target = nemesis_target # Maybe add accuracy check later
                 debug_log(f"AI {self.name}: Targeting Nemesis {target.name} with Green Shell")
             elif racers_ahead:
                 target = random.choice(racers_ahead) # Simple targetting
             if target:
                return "use_item", target.name # Target name
        elif self.current_item == Item.RED_SHELL:
            # Use if someone is ahead. Prioritize Nemesis.
             if nemesis_target and nemesis_target in racers_ahead:
                 target = nemesis_target
                 debug_log(f"AI {self.name}: Targeting Nemesis {target.name} with Red Shell")
             elif racers_ahead:
                 # Find closest racer ahead
                 target = min(racers_ahead, key=lambda r: r.position)
             if target:
                return "use_item", target.name
        elif self.current_item == Item.BANANA:
            # Drop if someone is close behind, or maybe just randomly?
            # Nemesis: Drop if nemesis is close behind?
            racers_close_behind = [r for r in racers_behind if self.position - r.position < 50] # Example range
            if nemesis_target and nemesis_target in racers_close_behind:
                 debug_log(f"AI {self.name}: Dropping Banana defensively against Nemesis {nemesis_target.name}")
                 return "use_item", None # Drop behind self
            elif racers_close_behind and random.random() < 0.7: # High chance if someone close behind
                 return "use_item", None
            elif Trait.AGGRESSIVE in self.traits and random.random() < 0.3: # Aggressive AI might drop randomly
                 return "use_item", None


        # Default: Keep driving if no good use case found yet
        return "drive", None


    def update_step(self, game_state):
        # Handle status effects first
        current_speed = self.base_speed
        if self.hit_timer > 0:
            self.hit_timer -= 1
            # Apply speed penalty based on what hit us
            penalty = 0
            duration = 0
            if self.last_hit_by_item == Item.GREEN_SHELL or self.last_hit_by_item == Item.RED_SHELL:
                penalty = CONFIG["shell_hit_speed_penalty"]
                #duration = CONFIG["shell_hit_duration"] # Duration handled by timer
            elif self.last_hit_by_item == Item.BANANA:
                 penalty = CONFIG["banana_hit_speed_penalty"]
                 #duration = CONFIG["banana_hit_duration"]

            # Trait modification: Shell-Shocked
            if Trait.SHELL_SHOCKED in self.traits:
                 penalty *= 1.5 # Slower for longer or more impact? Simple: More impact.

            current_speed += penalty
            if self.hit_timer == 0:
                debug_log(f"{self.name} recovered from hit.")
                self.last_hit_by = None
                self.last_hit_by_item = None

        elif self.boost_timer > 0:
            current_speed += CONFIG["boost_speed_bonus"]
            self.boost_timer -= 1
            if self.boost_timer == 0:
                 debug_log(f"{self.name}'s boost ended.")

        # Ensure speed doesn't go below a minimum reasonable value (e.g., 0 or slightly above)
        current_speed = max(1, current_speed) # Can't go backwards unless explicitly designed

        # Update position
        new_position = self.position + current_speed
        self.position = new_position

        # Check for hitting item boxes
        if self.position >= self.next_item_box_pos:
            if not self.current_item: # Can only pick up if hand is empty
                self.get_item(game_state)
            self.next_item_box_pos += CONFIG["item_box_spacing"] # Set next target box


    def get_item(self, game_state):
        # Simple item distribution - maybe add catch-up later
        boost_chance = CONFIG["item_chance_boost"]
        shell_chance = CONFIG["item_chance_green_shell"]
        red_shell_chance = CONFIG["item_chance_red_shell"]
        banana_chance = CONFIG["item_chance_banana"]

        # Catch-up assist
        leader_pos = game_state['positions'][-1][1] if game_state['positions'] else self.position
        if leader_pos - self.position > CONFIG["catch_up_assist_threshold"]:
            debug_log(f"{self.name} is far behind, applying catch-up item bonus.")
            boost_chance *= CONFIG["catch_up_item_boost_mult"]
            # Re-normalize probabilities if needed, or just increase boost chance relative to others
            # Simple approach: just boost the boost chance, let random selection handle it.


        rand_val = random.random()
        if rand_val < boost_chance:
            self.current_item = Item.BOOST
        elif rand_val < boost_chance + shell_chance:
            self.current_item = Item.GREEN_SHELL
        elif rand_val < boost_chance + shell_chance + red_shell_chance:
            self.current_item = Item.RED_SHELL
        else:
            self.current_item = Item.BANANA
        debug_log(f"{self.name} got item: {self.current_item}")


    def use_item(self, target_name, game_state):
        if not self.current_item:
            return False # No item to use

        item_used = self.current_item
        self.current_item = None
        self.item_uses[item_used] += 1
        debug_log(f"{self.name} used {item_used}" + (f" targeting {target_name}" if target_name else ""))


        # --- Item Effects ---
        if item_used == Item.BOOST:
            self.boost_timer = CONFIG["boost_duration"]
            debug_log(f"{self.name} started boosting!")

        elif item_used == Item.GREEN_SHELL:
            # Simple: Hit target if specified and exists. Add inaccuracy later?
            target_racer = game_state['racers'].get(target_name)
            if target_racer:
                 game_state['pending_events'].append({
                     "type": "hit", "attacker": self.name, "target": target_racer.name,
                     "item": item_used
                 })
                 # Update Nemesis hit counts immediately upon USE targeting someone
                 self.hit_others_count[target_racer.name] = self.hit_others_count.get(target_racer.name, 0) + 1

        elif item_used == Item.RED_SHELL:
             # Homing: More likely to hit the intended target ahead
             target_racer = game_state['racers'].get(target_name)
             if target_racer and target_racer.position > self.position: # Must be ahead
                 game_state['pending_events'].append({
                     "type": "hit", "attacker": self.name, "target": target_racer.name,
                     "item": item_used
                 })
                 self.hit_others_count[target_racer.name] = self.hit_others_count.get(target_racer.name, 0) + 1
             else:
                 debug_log(f"{self.name}'s Red Shell fizzled (target invalid or behind).")


        elif item_used == Item.BANANA:
             # Place banana behind the racer
             banana_pos = self.position - (self.base_speed / 2) # Place slightly behind
             game_state['obstacles'].append({"type": Item.BANANA, "position": banana_pos, "owner": self.name})
             debug_log(f"{self.name} dropped a Banana at position {banana_pos:.1f}")

        return True # Item was used


    def apply_hit(self, attacker_name, item_type, game_state):
        if self.hit_timer > 0: # Don't get hit if already stunned (grace period)
             debug_log(f"{self.name} has hit immunity, dodged {item_type}.")
             return

        debug_log(f"{self.name} was hit by {attacker_name}'s {item_type}!")
        self.boost_timer = 0 # Stop boosting if hit
        self.last_hit_by = attacker_name
        self.last_hit_by_item = item_type
        self.times_hit_by[item_type] += 1
        if attacker_name: # Can be hit by own banana or unattributed obstacle
            self.hit_by_count[attacker_name] = self.hit_by_count.get(attacker_name, 0) + 1

        # Set hit duration based on item
        if item_type == Item.GREEN_SHELL or item_type == Item.RED_SHELL:
             self.hit_timer = CONFIG["shell_hit_duration"]
        elif item_type == Item.BANANA:
             self.hit_timer = CONFIG["banana_hit_duration"]

        # --- Nemesis Relationship Update ---
        if attacker_name:
             # The one hit dislikes the attacker
             self.update_relationship(attacker_name, CONFIG["nemesis_hit_relationship_penalty"])
             debug_log(f"Nemesis: {self.name}'s relationship towards {attacker_name} decreased to {self.relationships.get(attacker_name, 0)}")
             # Attacker might gain 'satisfaction' or rivalry? Less direct impact for simple model.
             attacker_racer = game_state['racers'].get(attacker_name)
             if attacker_racer:
                  # Attacker feels slightly more rivalry towards the one they hit? Optional.
                  # attacker_racer.update_relationship(self.name, -1)
                  pass

        # Check if traits should be gained after being hit
        self.check_trait_conditions()


# --- Game Simulation Class ---
class Game:
    def __init__(self, config):
        self.config = config
        self.racers = {}
        self.player_name = "Player"
        self.winner = None
        self.step_count = 0
        self.game_over = False
        self.last_positions = {} # For overtake checks

        # Game world state
        self.obstacles = [] # List of {"type": Item.BANANA, "position": float, "owner": str}

        # Setup Racers
        racer_names = [f"CPU_{i+1}" for i in range(config["num_racers"] - 1)]
        if config["player_controlled"]:
            player = Racer(self.player_name, is_player=True)
            self.racers[player.name] = player
        else:
             # Add a regular AI racer if player isn't playing
             cpu_player = Racer(self.player_name, is_player=False)
             self.racers[cpu_player.name] = cpu_player


        for name in racer_names:
            self.racers[name] = Racer(name)

        all_racer_list = list(self.racers.values())
        for r in self.racers.values():
            r.initialize_relationships(all_racer_list)

    def get_state(self):
         # Provides necessary info for AI decisions and event processing
         sorted_racers = sorted(self.racers.values(), key=lambda r: r.position, reverse=True)
         positions = [(r.name, r.position) for r in sorted_racers]
         return {
             "racers": self.racers,
             "positions": positions, # List of (name, pos) tuples, sorted
             "obstacles": self.obstacles,
             "pending_events": [], # Events generated this step (hits, etc.)
             "track_length": self.config["track_length"],
             "step": self.step_count,
         }

    def run_step(self, player_action=None, player_target=None):
        if self.game_over:
            return True

        self.step_count += 1
        debug_log(f"\n--- Step {self.step_count} ---")

        current_game_state = self.get_state()
        actions = {} # racer_name: (action_type, target_name)

        # 1. Decide Actions (AI + Player)
        for name, racer in self.racers.items():
            if racer.is_player:
                if player_action:
                    actions[name] = (player_action, player_target)
                else:
                    actions[name] = ("drive", None) # Default if no input
            else:
                # Store AI decisions
                actions[name] = racer.decide_action(current_game_state)


        # Store previous positions before update
        self.last_positions = {name: r.position for name, r in self.racers.items()}


        # 2. Execute Actions & Update Positions
        for name, racer in self.racers.items():
             action, target = actions[name]
             if action == "use_item":
                 racer.use_item(target, current_game_state) # This adds events to pending_events

             # Update movement regardless of action (unless hit)
             racer.update_step(current_game_state)


        # 3. Process Pending Events (Hits)
        for event in current_game_state['pending_events']:
            if event['type'] == 'hit':
                target_racer = self.racers.get(event['target'])
                if target_racer:
                    target_racer.apply_hit(event['attacker'], event['item'], current_game_state)

        # 4. Check Obstacle Collisions (Bananas)
        new_obstacles = []
        racers_to_check = list(self.racers.values()) # Check against current positions
        for obs in self.obstacles:
            hit_obstacle = False
            for racer in racers_to_check:
                 # Check if racer moved over the obstacle in this step
                 last_pos = self.last_positions.get(racer.name, racer.position)
                 current_pos = racer.position
                 # Simple check: did the interval [last_pos, current_pos] cross obs['position']?
                 if min(last_pos, current_pos) < obs['position'] <= max(last_pos, current_pos):
                     if racer.hit_timer <= 0: # Can't hit if already stunned
                         debug_log(f"Collision: {racer.name} hit {obs['owner']}'s {obs['type']} at {obs['position']:.1f}")
                         racer.apply_hit(obs['owner'], obs['type'], current_game_state)
                         hit_obstacle = True
                         # Optional: remove racer from further checks this step if needed
                         # racers_to_check.remove(racer)
                         break # Obstacle is used up
            if not hit_obstacle:
                 new_obstacles.append(obs) # Keep obstacle if not hit
        self.obstacles = new_obstacles


        # 5. Check for Overtakes (Nemesis Update)
        current_sorted_racers = sorted(self.racers.values(), key=lambda r: r.position, reverse=True)
        last_sorted_racers_names = [r[0] for r in sorted(self.last_positions.items(), key=lambda item: item[1], reverse=True)]

        for i, current_racer in enumerate(current_sorted_racers):
             try:
                 last_rank_index = last_sorted_racers_names.index(current_racer.name)
                 if i < last_rank_index: # Racer improved rank
                     # Check who they overtook
                     for j in range(i + 1, last_rank_index + 1):
                          # Potential bug here if list lengths change, be careful
                          if j < len(last_sorted_racers_names):
                            overtaken_racer_name = last_sorted_racers_names[j]
                            overtaken_racer = self.racers.get(overtaken_racer_name)
                            if overtaken_racer:
                                debug_log(f"Overtake: {current_racer.name} overtook {overtaken_racer_name}")
                                # The one overtaken feels negative towards the overtaker
                                overtaken_racer.update_relationship(current_racer.name, CONFIG["nemesis_overtake_relationship_penalty"])
                                debug_log(f"Nemesis: {overtaken_racer_name}'s relationship towards {current_racer.name} decreased to {overtaken_racer.relationships.get(current_racer.name, 0)}")
             except ValueError:
                  pass # Racer wasn't in the list last time? Should not happen in normal race.


        # 6. Check Win Condition
        for name, racer in self.racers.items():
            if racer.position >= self.config["track_length"]:
                self.winner = name
                self.game_over = True
                debug_log(f"\n!!! {name} wins the race! !!!")
                # Final Nemesis Update based on finishing order
                winner_racer = self.racers[self.winner]
                sorted_racers = sorted(self.racers.values(), key=lambda r: r.position, reverse=True)
                for i, r in enumerate(sorted_racers):
                    if r.name != self.winner:
                         # Losers feel negative towards winner & those ahead
                         for j in range(i):
                             finisher_ahead = sorted_racers[j]
                             r.update_relationship(finisher_ahead.name, CONFIG["nemesis_finish_ahead_penalty"])
                             debug_log(f"Nemesis (Finish): {r.name}'s relationship towards {finisher_ahead.name} decreased to {r.relationships.get(finisher_ahead.name, 0)}")
                break # End step once winner found

        # 7. Update Traits based on accumulated stats (do this periodically or at end?)
        # Doing it here each step is simple for now
        for racer in self.racers.values():
             racer.check_trait_conditions()

        return self.game_over


    def print_status(self):
        print(f"\n--- Race Status (Step {self.step_count}) ---")
        sorted_racers = sorted(self.racers.values(), key=lambda r: r.position, reverse=True)
        print("Place | Name        | Position | Speed | Item          | Boost | Hit | Traits")
        print("------|-------------|----------|-------|---------------|-------|-----|---------------")
        for i, r in enumerate(sorted_racers):
            pos_str = f"{r.position:.1f}/{self.config['track_length']}"
            item_str = r.current_item if r.current_item else "None"
            boost_str = f"Yes ({r.boost_timer})" if r.boost_timer > 0 else "No"
            hit_str = f"Yes ({r.hit_timer})" if r.hit_timer > 0 else "No"
            traits_str = ", ".join(r.traits) if r.traits else "None"
            print(f"{i+1:<5} | {r.name:<11} | {pos_str:<8} | {r.speed:<5.1f} | {item_str:<13} | {boost_str:<5} | {hit_str:<3} | {traits_str}")

        # Print Obstacles
        if self.obstacles:
             print("Obstacles on track:")
             for obs in self.obstacles:
                 print(f"  - {obs['type']} at {obs['position']:.1f} (Owner: {obs['owner']})")

    def get_racer_details(self, name):
         racer = self.racers.get(name)
         if not racer:
             return f"Racer '{name}' not found."

         details = f"--- Details for {name} ---\n"
         details += f"Position: {racer.position:.1f}\n"
         details += f"Base Speed: {racer.base_speed:.1f}\n"
         details += f"Current Item: {racer.current_item}\n"
         details += f"Boost Timer: {racer.boost_timer}\n"
         details += f"Hit Timer: {racer.hit_timer}\n"
         details += f"Last Hit By: {racer.last_hit_by} ({racer.last_hit_by_item})\n"
         details += "Traits: " + (", ".join(racer.traits) if racer.traits else "None") + "\n"
         details += "Item Uses:\n"
         for item, count in racer.item_uses.items():
             details += f"  - {item}: {count}\n"
         details += "Times Hit By Item:\n"
         for item, count in racer.times_hit_by.items():
             details += f"  - {item}: {count}\n"
         details += "Relationships:\n"
         if racer.relationships:
             for other_name, score in sorted(racer.relationships.items(), key=lambda item: item[1]):
                 details += f"  - Towards {other_name}: {score}\n"
         else:
             details += "  None\n"
         details += "Hit By Count:\n"
         if racer.hit_by_count:
             for attacker_name, count in racer.hit_by_count.items():
                 if count > 0: details += f"  - From {attacker_name}: {count}\n"
         else:
             details += "  None\n"
         details += "Hit Others Count:\n"
         if racer.hit_others_count:
             for target_name, count in racer.hit_others_count.items():
                 if count > 0: details += f"  - Towards {target_name}: {count}\n"
         else:
             details += "  None\n"

         return details


# --- Debug Terminal & Input Handling ---
debug_log_buffer = queue.Queue()
stop_event = threading.Event()
input_queue = queue.Queue()

def debug_log(message):
    """Adds a message to the debug log buffer."""
    debug_log_buffer.put(message)

def print_debug_output():
    """Prints messages from the debug log buffer."""
    while not debug_log_buffer.empty():
        print(f"[DEBUG] {debug_log_buffer.get_nowait()}")

def input_thread_func():
    """Thread function to handle user input without blocking."""
    while not stop_event.is_set():
        try:
            command = input()
            input_queue.put(command)
        except EOFError: # Handle ctrl+d or end of input stream
             stop_event.set()
             break
        time.sleep(0.1) # Prevent busy-waiting


# --- Main Simulation Loop ---
if __name__ == "__main__":
    game = Game(CONFIG)
    running_simulation = False
    auto_step = False

    print("--- Mario Kart Nemesis Simulator ---")
    print("Commands:")
    print("  run          - Run the simulation automatically until the end.")
    print("  step [n=1]   - Advance the simulation by n steps.")
    print("  status [name]- Show racer status (or all if no name).")
    print("  config [key] [value] - View or set a config value.")
    print("  give [racer] [item] - Give an item (Boost, Green_Shell, Red_Shell, Banana).")
    print("  use [item] [target?] - Player uses item (target optional for shells).")
    print("  rel [r1] [r2] [val] - Set relationship score for r1 towards r2.")
    print("  debug        - Toggle showing debug messages (Default: ON).")
    print("  help         - Show this help message.")
    print("  quit         - Exit the simulator.")
    print("------------------------------------")

    show_debug = True
    player_command = None
    player_target_arg = None

    # Start input thread
    inp_thread = threading.Thread(target=input_thread_func, daemon=True)
    inp_thread.start()

    try:
        while not game.game_over and not stop_event.is_set():
            # Process Input Commands
            while not input_queue.empty():
                command_line = input_queue.get_nowait()
                parts = command_line.split()
                if not parts: continue
                cmd = parts[0].lower()

                if cmd == "quit":
                    stop_event.set()
                    break
                elif cmd == "run":
                    auto_step = True
                    print("Running simulation automatically...")
                elif cmd == "step":
                    num_steps = 1
                    if len(parts) > 1 and parts[1].isdigit():
                        num_steps = int(parts[1])
                    for _ in range(num_steps):
                         if game.game_over: break
                         game.run_step(player_command, player_target_arg)
                         player_command = None # Consume player command
                         player_target_arg = None
                         game.print_status()
                         if show_debug: print_debug_output()
                    auto_step = False # Stop auto-stepping after manual steps
                elif cmd == "status":
                     if len(parts) > 1:
                         print(game.get_racer_details(parts[1]))
                     else:
                         game.print_status()
                elif cmd == "config":
                     if len(parts) == 1:
                         print("--- Current Configuration ---")
                         for key, value in CONFIG.items():
                             print(f"{key}: {value}")
                         print("---------------------------")
                     elif len(parts) == 2:
                          key = parts[1]
                          if key in CONFIG:
                              print(f"{key}: {CONFIG[key]}")
                          else:
                              print(f"Unknown config key: {key}")
                     elif len(parts) == 3:
                         key, value_str = parts[1], parts[2]
                         if key in CONFIG:
                             try:
                                 # Attempt to convert to the original type
                                 original_type = type(CONFIG[key])
                                 new_value = original_type(value_str)
                                 CONFIG[key] = new_value
                                 print(f"Set {key} = {new_value}")
                                 # Re-initialize game if critical config changed? For simplicity, no.
                                 # Could add checks for things like num_racers requiring restart.
                             except ValueError:
                                 print(f"Invalid value type for {key}. Expected {original_type.__name__}.")
                         else:
                             print(f"Unknown config key: {key}")
                     else:
                         print("Usage: config [key] [value] or config [key] or config")

                elif cmd == "give":
                     if len(parts) == 3:
                         racer_name, item_name_part = parts[1], parts[2]
                         racer = game.racers.get(racer_name)
                         # Map input string to Item enum robustly
                         item_to_give = None
                         for item_val in [Item.BOOST, Item.GREEN_SHELL, Item.RED_SHELL, Item.BANANA]:
                              if item_name_part.lower() in item_val.lower().replace(" ", "_"):
                                   item_to_give = item_val
                                   break

                         if racer and item_to_give:
                             racer.current_item = item_to_give
                             print(f"Gave {item_to_give} to {racer_name}.")
                             debug_log(f"DEBUG CMD: Gave {item_to_give} to {racer_name}.")
                         elif not racer:
                             print(f"Racer '{racer_name}' not found.")
                         else:
                             print(f"Invalid item name: {item_name_part}. Use Boost, Green_Shell, Red_Shell, or Banana.")
                     else:
                         print("Usage: give [racer_name] [item_name]")

                elif cmd == "use":
                    if not CONFIG["player_controlled"]:
                         print("Player is not enabled (CONFIG['player_controlled']=False).")
                         continue
                    player_racer = game.racers.get(game.player_name)
                    if not player_racer:
                         print("Player racer not found!") # Should not happen
                         continue

                    if not player_racer.current_item:
                         print("Player has no item to use.")
                         continue

                    item_to_use = player_racer.current_item # Item determined by what player holds

                    # Determine target based on item and command args
                    target_name_arg = None
                    if item_to_use in [Item.GREEN_SHELL, Item.RED_SHELL] and len(parts) > 1:
                        target_name_arg = parts[1]
                        # Validate target exists? The use_item function does basic validation.
                        if target_name_arg not in game.racers:
                             print(f"Target racer '{target_name_arg}' not found. Item may fizzle.")
                             # Allow attempting anyway, maybe add better validation later

                    # Set player action for the *next* step
                    player_command = "use_item"
                    player_target_arg = target_name_arg
                    print(f"Player action set: Use {item_to_use}" + (f" targeting {target_name_arg}" if target_name_arg else ""))
                    # Don't advance step here, wait for 'step' or 'run' command

                elif cmd == "rel":
                     if len(parts) == 4:
                         r1_name, r2_name, val_str = parts[1], parts[2], parts[3]
                         r1 = game.racers.get(r1_name)
                         r2 = game.racers.get(r2_name)
                         if r1 and r2 and r1 != r2:
                             try:
                                 value = int(val_str)
                                 r1.update_relationship(r2_name, value - r1.relationships.get(r2_name, 0)) # Set absolute value
                                 print(f"Set {r1_name}'s relationship towards {r2_name} to {r1.relationships[r2_name]}.")
                                 debug_log(f"DEBUG CMD: Set relationship {r1_name}->{r2_name} = {r1.relationships[r2_name]}.")
                             except ValueError:
                                 print("Invalid relationship value, must be an integer.")
                         elif not r1: print(f"Racer '{r1_name}' not found.")
                         elif not r2: print(f"Racer '{r2_name}' not found.")
                         else: print("Cannot set relationship to self.")
                     else:
                         print("Usage: rel [racer1] [racer2] [value]")

                elif cmd == "debug":
                    show_debug = not show_debug
                    print(f"Debug messages {'ON' if show_debug else 'OFF'}")
                elif cmd == "help":
                     print("Commands: run, step [n], status [name], config [k] [v], give [r] [i], use [i] [t?], rel [r1] [r2] [v], debug, help, quit")
                else:
                    print(f"Unknown command: {cmd}. Type 'help' for list.")

            # Automatic Simulation Step if 'run' was entered
            if auto_step and not game.game_over:
                game_over = game.run_step(player_command, player_target_arg)
                player_command = None # Consume player command after step
                player_target_arg = None
                game.print_status()
                if show_debug: print_debug_output()
                if game_over:
                    auto_step = False # Stop running automatically when game ends
                else:
                    time.sleep(CONFIG["simulation_step_delay"]) # Pause between auto steps

            elif not auto_step:
                 # If not auto-running, give a prompt indication if waiting for input
                 # (Handled implicitly by the input() call in the thread)
                 time.sleep(0.1) # Small sleep to prevent busy-looping when idle


    except KeyboardInterrupt:
        print("\nSimulation interrupted.")
    finally:
        print("Stopping simulation...")
        stop_event.set()
        # Wait briefly for input thread to notice stop_event
        time.sleep(0.2)
        # No join needed for daemon thread, but good practice if it weren't daemon
        # inp_thread.join()
        print("Simulation finished.")
        if game.winner:
             print(f"Winner: {game.winner}")
        else:
             print("Race did not finish.")

