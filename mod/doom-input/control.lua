--[[ doom-input: optional keyboard input for the factorio-doom blueprint.

The blueprint carries two vanilla constant combinators tagged by their description:
"doom-input:held" on the input pad's HELD network and "doom-input:pressed" on its PRESSED
network. Without this mod they stay empty and the chest pad works as before. While the shortcut
is toggled on, this mod writes into them, every tick their contents change:

  held     signal-W forward, S back, A turn left, D turn right (from the game's walking state, so
           whatever the movement keys are bound to, diagonals as two buttons), signal-F fire (the
           shooting state: Space by default). 1 while held.
  pressed  signal-N next weapon (the game's next-weapon control), signal-U use: 1 for one tick.

A script's write is on the wire the next tick (conformance case constant_script). The game only
reports key presses, never releases, so held keys come from walking_state / shooting_state, which
follow a press one tick late; the press event itself fills that tick. Movement keys also move the
player (the camera, in the editor or god mode), so while on, the player is teleported back each
tick to where it was when toggled on.
]]

local HELD_TAG, PRESSED_TAG = "doom-input:held", "doom-input:pressed"
local FORWARD, BACK, LEFT, RIGHT, FIRE = "signal-W", "signal-S", "signal-A", "signal-D", "signal-F"
local NEXT, USE = "signal-N", "signal-U"
local HELD_ORDER = { FORWARD, BACK, LEFT, RIGHT, FIRE }
local RESCAN = 600 -- ticks between scans for tagged combinators while on

local d = defines.direction
local WALK = {
  [d.north] = { FORWARD }, [d.northeast] = { FORWARD, RIGHT }, [d.east] = { RIGHT },
  [d.southeast] = { BACK, RIGHT }, [d.south] = { BACK }, [d.southwest] = { BACK, LEFT },
  [d.west] = { LEFT }, [d.northwest] = { FORWARD, LEFT },
}
local LINKED = {
  ["doom-input-move-up"] = FORWARD, ["doom-input-move-down"] = BACK,
  ["doom-input-move-left"] = LEFT, ["doom-input-move-right"] = RIGHT,
  ["doom-input-shoot-enemy"] = FIRE,
}
local PRESSES = { ["doom-input-next-weapon"] = NEXT, ["doom-input-use"] = USE }

local function S()
  storage.active = storage.active or {}         -- player index -> {anchor, controller, pending = {sig = tick}}
  storage.combs = storage.combs or { held = {}, pressed = {} }
  storage.written = storage.written or { held = "", pressed = "" }
  storage.pulses = storage.pulses or {}         -- sig -> tick pressed
  storage.test = storage.test or {}             -- test override (remote interface)
  return storage
end

local function scan()
  local s = S()
  s.combs = { held = {}, pressed = {} }
  for _, surface in pairs(game.surfaces) do
    for _, c in pairs(surface.find_entities_filtered({ type = "constant-combinator" })) do
      local tag = c.combinator_description
      if tag == HELD_TAG then table.insert(s.combs.held, c)
      elseif tag == PRESSED_TAG then table.insert(s.combs.pressed, c) end
    end
  end
  s.written = { held = "", pressed = "" }  -- rewrite on the next update
  return #s.combs.held + #s.combs.pressed
end

local function write(kind, signals)
  local s = S()
  local key = table.concat(signals, ",")
  if key == s.written[kind] then return end
  s.written[kind] = key
  local filters = {}
  for i, name in ipairs(signals) do
    filters[i] = { value = { type = "virtual", name = name, quality = "normal", comparator = "=" }, min = 1 }
  end
  for _, c in pairs(s.combs[kind]) do
    if c.valid then
      local cb = c.get_control_behavior()
      local sec = cb.get_section(1) or cb.add_section()
      if sec and sec.is_manual then sec.filters = filters end
    end
  end
end

local function active_players()
  local out = {}
  for index, p in pairs(S().active) do
    local player = game.get_player(index)
    if player and player.connected then out[#out + 1] = { player = player, st = p } end
  end
  return out
end

-- What the combinators should hold at `tick`, then write it if it changed.
local function update(tick)
  local s = S()
  local held, pressed = {}, {}
  for _, a in pairs(active_players()) do
    local p = a.player
    local ok, w = pcall(function() return p.walking_state end)
    if ok and w and w.walking then
      for _, sig in pairs(WALK[w.direction] or {}) do held[sig] = true end
    end
    local ok2, sh = pcall(function() return p.shooting_state end)
    if ok2 and sh and sh.state ~= defines.shooting.not_shooting then held[FIRE] = true end
    for sig, t in pairs(a.st.pending) do
      -- the state follows a press a tick later; keep the press through that tick too
      if t + 1 >= tick then held[sig] = true else a.st.pending[sig] = nil end
    end
  end
  for sig in pairs(s.test.held or {}) do held[sig] = true end
  for sig, t in pairs(s.pulses) do
    if t == tick then pressed[sig] = true else s.pulses[sig] = nil end
  end
  local hl, pl = {}, {}
  for _, sig in ipairs(HELD_ORDER) do if held[sig] then hl[#hl + 1] = sig end end
  for _, sig in ipairs({ NEXT, USE }) do if pressed[sig] then pl[#pl + 1] = sig end end
  write("held", hl)
  write("pressed", pl)
end

local function lock(player, st)
  if not st.anchor or player.controller_type ~= st.controller then return end
  local pos = player.position
  if pos.x ~= st.anchor.x or pos.y ~= st.anchor.y then player.teleport(st.anchor) end
end

local function set_enabled(player, on)
  local s = S()
  if on then
    local n = scan()
    s.active[player.index] = { anchor = player.position, controller = player.controller_type, pending = {} }
    if n == 0 then
      player.print("Doom keyboard input: no keyboard combinators found (constant combinators described \"doom-input:held\" / \"doom-input:pressed\"). Paste a blueprint built with keyboard support.")
    end
  else
    s.active[player.index] = nil
  end
  player.set_shortcut_toggled("doom-input-toggle", on)
  update(game.tick)
end

local function toggle(e)
  local player = game.get_player(e.player_index)
  if player then set_enabled(player, not S().active[player.index]) end
end

script.on_init(S)
script.on_configuration_changed(function()
  -- The 0.1 probe kept per-player data in storage.players and a panel on screen.
  storage.players = nil
  for _, p in pairs(game.players) do
    if p.gui.screen["doom-probe"] then p.gui.screen["doom-probe"].destroy() end
  end
  S()
  scan()
end)

script.on_event(defines.events.on_lua_shortcut, function(e)
  if e.prototype_name == "doom-input-toggle" then toggle(e) end
end)
script.on_event("doom-input-toggle", toggle)

script.on_event(defines.events.on_tick, function(e)
  local s = S()
  if next(s.active) == nil and next(s.test) == nil and next(s.pulses) == nil
      and s.written.held == "" and s.written.pressed == "" then return end
  if e.tick % RESCAN == 0 then scan() end
  for _, a in pairs(active_players()) do lock(a.player, a.st) end
  update(e.tick)
end)

local function on_press(e)
  local st = S().active[e.player_index]
  if not st then return end
  local sig = LINKED[e.input_name]
  if sig then
    st.pending[sig] = e.tick
  else
    S().pulses[PRESSES[e.input_name]] = e.tick
  end
  update(e.tick)
end
local names = {}
for n in pairs(LINKED) do names[#names + 1] = n end
for n in pairs(PRESSES) do names[#names + 1] = n end
script.on_event(names, on_press)

-- The camera lock. The game moves the player after on_tick, so a teleport there leaves every
-- held tick's move on screen; this event comes right after the move (once it crosses a tile
-- edge), before the frame is drawn, so the move never shows. on_tick still catches moves that
-- stay within a tile (zoomed far in, where the camera moves slowly).
script.on_event(defines.events.on_player_changed_position, function(e)
  local st = S().active[e.player_index]
  local player = game.get_player(e.player_index)
  if st and player then lock(player, st) end
end)

-- Pasted or rebuilt combinators: rescan while anyone has it on.
local function built(e)
  local ent = e.entity or e.destination
  if next(S().active) and ent and ent.valid and ent.type == "constant-combinator" then scan() end
end
for _, ev in pairs({ defines.events.on_built_entity, defines.events.on_robot_built_entity,
                     defines.events.script_raised_built, defines.events.on_entity_cloned }) do
  script.on_event(ev, built)
end
script.on_event(defines.events.on_gui_closed, function(e)
  if e.entity and e.entity.valid and e.entity.type == "constant-combinator" then scan() end
end)

-- For tests (the harness): hold buttons or press one without a keyboard.
remote.add_interface("doom-input", {
  scan = scan,
  hold = function(signals)
    local s = S()
    s.test.held = {}
    for _, sig in pairs(signals or {}) do s.test.held[sig] = true end
    if next(s.test.held) == nil then s.test.held = nil end
    update(game.tick)
  end,
  press = function(sig)
    S().pulses[sig] = game.tick
    update(game.tick)
  end,
})
