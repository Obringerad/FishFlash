-- Fish Flash
-- Companion addon for the Fish Flash screen watcher (the watcher folder of this download).
--
-- WoW gives addons no "fish on the hook" event, so the bite is spotted outside the game: the
-- watcher program looks at your screen and flashes it when the bobber dips. This addon tells
-- the watcher when you're fishing by showing a tiny magenta square in the top-left corner of
-- the screen while the Fishing channel is running. The watcher only looks for bites while the
-- square is showing, so nothing else on screen can set it off.
--
-- Optional: /fishflash boost turns sound effects up (and music/ambience down) while fishing.

local ADDON_NAME = ...
local GOLD = "|cfff0c050"

local FISHING_IDS = { [7620] = true, [7731] = true, [7732] = true, [18248] = true, [33095] = true,
  [51294] = true, [88868] = true, [110410] = true, [131474] = true, [131476] = true }
local FISHING_ICON = 136245 -- Interface\Icons\Trade_Fishing

local MARKER_SIZE = 12 -- UI units; the watcher looks for this solid magenta (255, 0, 255) square

local SOUND_BOOST = {
  Sound_EnableSFX = "1",
  Sound_SFXVolume = "1.0",
  Sound_MusicVolume = "0",
  Sound_AmbienceVolume = "0",
  Sound_EnableSoundWhenGameIsInBG = "1",
}

local DEFAULTS = {
  enabled = true,
  boost = false,
}

local db
local fishing = false

local function say(msg)
  DEFAULT_CHAT_FRAME:AddMessage(GOLD .. "Fish Flash:|r " .. msg)
end

-- Call fn safely; returns nothing if it errors.
local function safe(fn, ...)
  if not fn then return nil end
  local ok, a, b, c, d, e, f, g, h = pcall(fn, ...)
  if not ok then return nil end
  return a, b, c, d, e, f, g, h
end

local function isSecret(v) return issecretvalue and issecretvalue(v) end

local function getCVar(name)
  if C_CVar and C_CVar.GetCVar then return safe(C_CVar.GetCVar, name) end
  return safe(GetCVar, name)
end

local function setCVar(name, value)
  if C_CVar and C_CVar.SetCVar then return safe(C_CVar.SetCVar, name, value) end
  return safe(SetCVar, name, value)
end

-- ---------------------------------------------------------------------------
-- The marker the watcher looks for. No parent, so it stays visible with the UI hidden (Alt+Z).
-- ---------------------------------------------------------------------------
local marker = CreateFrame("Frame", "FishFlashMarker")
marker:SetFrameStrata("TOOLTIP")
marker:SetFrameLevel(10000)
marker:SetSize(MARKER_SIZE, MARKER_SIZE)
marker:SetPoint("TOPLEFT", WorldFrame, "TOPLEFT", 0, 0)
marker:EnableMouse(false)
local markerTex = marker:CreateTexture(nil, "OVERLAY")
markerTex:SetAllPoints()
markerTex:SetColorTexture(1, 0, 1, 1)
marker:Hide()

local testUntil = 0
local function UpdateMarker()
  if (fishing and db and db.enabled) or GetTime() < testUntil then marker:Show() else marker:Hide() end
end

-- ---------------------------------------------------------------------------
-- Sound boost (saved in db so a /reload or crash mid-cast still gets restored)
-- ---------------------------------------------------------------------------
local function BoostSound()
  if not db.boost or db.savedCVars then return end
  local saved = {}
  for name, value in pairs(SOUND_BOOST) do
    local cur = getCVar(name)
    if cur ~= nil then
      saved[name] = cur
      setCVar(name, value)
    end
  end
  db.savedCVars = saved
end

local function RestoreSound()
  if not db.savedCVars then return end
  for name, value in pairs(db.savedCVars) do setCVar(name, value) end
  db.savedCVars = nil
end

-- ---------------------------------------------------------------------------
-- Is the player channeling Fishing?
-- ---------------------------------------------------------------------------
local fishingName
local function FishingName()
  if fishingName then return fishingName end
  local n = (C_Spell and C_Spell.GetSpellName and safe(C_Spell.GetSpellName, 131474))
    or (GetSpellInfo and safe(GetSpellInfo, 7620))
  if n and not isSecret(n) then fishingName = n end
  return fishingName or "Fishing"
end

local function IsFishingChannel()
  local name, _, texture, _, _, _, _, spellID = safe(UnitChannelInfo, "player")
  local ok, result = pcall(function()
    if spellID and FISHING_IDS[spellID] then return true end
    if texture and (texture == FISHING_ICON or (type(texture) == "string" and texture:lower():find("trade_fishing"))) then
      return true
    end
    return name ~= nil and name == FishingName()
  end)
  return ok and result
end

-- ---------------------------------------------------------------------------
-- Cast tracking
-- ---------------------------------------------------------------------------
local function OnFishingStart()
  fishing = true
  UpdateMarker()
  BoostSound()
end

local function OnFishingStop()
  if not fishing then return end
  fishing = false
  UpdateMarker()
  -- give the loot sound a moment before turning the music back on; keep the boost if they recast
  if C_Timer then
    C_Timer.After(2, function() if not fishing then RestoreSound() end end)
  else
    RestoreSound()
  end
end

local f = CreateFrame("Frame")
f:RegisterEvent("ADDON_LOADED")
f:RegisterEvent("PLAYER_LOGIN")
f:RegisterEvent("PLAYER_LOGOUT")
f:RegisterUnitEvent("UNIT_SPELLCAST_CHANNEL_START", "player")
f:RegisterUnitEvent("UNIT_SPELLCAST_CHANNEL_STOP", "player")
f:RegisterUnitEvent("UNIT_SPELLCAST_INTERRUPTED", "player")

f:SetScript("OnEvent", function(_, event, arg1)
  if event == "ADDON_LOADED" and arg1 == ADDON_NAME then
    FishFlashDB = FishFlashDB or {}
    db = FishFlashDB
    for k, v in pairs(DEFAULTS) do
      if db[k] == nil then db[k] = v end
    end
    -- settings from the first version, which tried to find a bite event in game
    db.biteEvent, db.lastDebugLog, db.sound, db.color, db.flashes = nil, nil, nil, nil, nil
  elseif event == "PLAYER_LOGIN" then
    RestoreSound() -- left over from a crash or /reload mid-cast
  elseif event == "PLAYER_LOGOUT" then
    RestoreSound()
  elseif not db then
    return
  elseif event == "UNIT_SPELLCAST_CHANNEL_START" then
    if IsFishingChannel() then OnFishingStart() end
  else
    OnFishingStop()
  end
end)

-- ---------------------------------------------------------------------------
-- /fishflash
-- ---------------------------------------------------------------------------
local function onOff(v) return v and "|cff60ff60on|r" or "|cffff6060off|r" end

local function Help()
  say("the screen watcher program does the flashing; this addon tells it when you're fishing.")
  say("  /fishflash on | off - show the fishing marker for the watcher (now " .. onOff(db.enabled) .. ")")
  say("  /fishflash marker - show the marker for 10 seconds, to check the watcher sees it")
  say("  /fishflash boost - louder effects, quieter music while fishing (now " .. onOff(db.boost) .. ")")
end

SLASH_FISHFLASH1 = "/fishflash"
SlashCmdList.FISHFLASH = function(msg)
  if not db then return end
  local cmd = ((msg or ""):match("^%s*(%S*)") or ""):lower()
  if cmd == "on" or cmd == "off" then
    db.enabled = cmd == "on"
    UpdateMarker()
    say("fishing marker " .. onOff(db.enabled))
  elseif cmd == "marker" then
    testUntil = GetTime() + 10
    UpdateMarker()
    if C_Timer then C_Timer.After(10.1, UpdateMarker) end
    say("showing the marker in the top-left corner for 10 seconds.")
  elseif cmd == "boost" then
    db.boost = not db.boost
    if not db.boost then RestoreSound() elseif fishing then BoostSound() end
    say("sound boost while fishing " .. onOff(db.boost))
  else
    Help()
  end
end
