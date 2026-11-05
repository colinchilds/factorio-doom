-- doom-input: the shortcut button and the inputs it listens to (control.lua has the details).
local P = "__doom-input__/graphics/"
local inputs = {
  {
    type = "shortcut",
    name = "doom-input-toggle",
    order = "z[doom-input]",
    action = "lua",
    toggleable = true,
    associated_control_input = "doom-input-toggle",
    icon = P .. "joystick-x56.png",
    icon_size = 56,
    small_icon = P .. "joystick-x24.png",
    small_icon_size = 24,
  },
  { type = "custom-input", name = "doom-input-toggle", key_sequence = "CONTROL + SHIFT + J", consuming = "none", action = "lua" },
  -- "Use", reset and the weapon slots have no game controls of their own; rebind them under
  -- Settings > Controls > Mods. The number keys also select quickbar slots (consuming = "none":
  -- taking them from the game would take them while the mod is off too).
  { type = "custom-input", name = "doom-input-use", key_sequence = "F", consuming = "none", action = "lua" },
  { type = "custom-input", name = "doom-input-reset", key_sequence = "CONTROL + SHIFT + R", consuming = "none", action = "lua" },
}
for k = 1, 7 do
  inputs[#inputs + 1] = { type = "custom-input", name = "doom-input-weapon-" .. k, key_sequence = tostring(k), consuming = "none", action = "lua" }
end
-- Links to the game's own controls, whatever they're bound to: a press shows up here the tick it
-- happens, before walking_state / shooting_state follow it.
for _, c in ipairs({ "move-up", "move-down", "move-left", "move-right", "shoot-enemy" }) do
  inputs[#inputs + 1] = { type = "custom-input", name = "doom-input-" .. c, key_sequence = "", linked_game_control = c, consuming = "none", action = "lua" }
end
data:extend(inputs)
