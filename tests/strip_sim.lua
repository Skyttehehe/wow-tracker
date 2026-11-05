-- Loads the addon against stubbed WoW APIs, simulates a few seconds of play
-- and writes every pixel-strip frame (171 bytes each) to a binary file.
-- Usage: lua tests/strip_sim.lua <frames.bin>

local out = arg[1]
local clock, gtime = 1793890000, 0
local SECRET = setmetatable({}, { __tostring = function() return "<secret>" end })

local S = { level = 3, xp = 120, xpMax = 1100, x = 0.2971, y = 0.7166, zone = "Dun Morogh",
	subzone = "Coldridge Valley", mapID = 1426, hp = SECRET, hpMax = 92, mana = 70, manaMax = 110 }
local T -- current target, set during the run

function issecretvalue(v) return v == SECRET end
function GetServerTime() return clock end
function GetTime() return gtime end
time = function() return clock end
function wipe(t) for k in pairs(t) do t[k] = nil end return t end
function hooksecurefunc() end
function GetPhysicalScreenSize() return 2560, 1440 end

local function texture()
	local t = {}
	setmetatable(t, { __index = function() return function() end end })
	return t
end
local frames = {}
function CreateFrame()
	local f = { scripts = {} }
	f.RegisterEvent = function() end
	f.UnregisterEvent = function() end
	f.SetScript = function(self, name, fn) self.scripts[name] = fn end
	f.CreateTexture = function() return texture() end
	setmetatable(f, { __index = function() return function() end end })
	frames[#frames + 1] = f
	return f
end
local tickers = {}
C_Timer = { NewTicker = function(_, fn) tickers[#tickers + 1] = fn end, After = function(_, fn) fn() end }
C_Map = {
	GetBestMapForUnit = function() return S.mapID end,
	GetPlayerMapPosition = function() return { GetXY = function() return S.x, S.y end } end,
	GetMapInfo = function() return { name = S.zone } end,
}
local quests = {
	{ title = "Dun Morogh", isHeader = true },
	{ title = "Dwarven Outfitters", questID = 179, level = 1 },
	{ title = "Coldridge Valley Mail Delivery", questID = 233, level = 2 },
	{ title = "A \"Tough\" Wolf Meat — Ünïcode", questID = 3361, level = 3 },
}
C_QuestLog = {
	GetNumQuestLogEntries = function() return #quests end,
	GetInfo = function(i) return quests[i] end,
	IsComplete = function(id) return id == 233 end,
	IsFailed = function() return false end,
	GetQuestObjectives = function(id)
		if id == 179 then return { { text = "Tough Wolf Meat: 6/8", finished = false, numFulfilled = 6, numRequired = 8 } } end
		if id == 3361 then return { { text = "Wolf slain", finished = true, numFulfilled = 1, numRequired = 1 } } end
		return {}
	end,
	GetTitleForQuestID = function(id) return "Quest " .. id end,
}
DEFAULT_CHAT_FRAME = { AddMessage = function(_, m) print("[chat] " .. m) end }
SlashCmdList = {}
UISpecialFrames = {}
function UnitClass() return "Shaman", "SHAMAN" end
function UnitRace() return "Dwarf" end
function UnitLevel() return S.level end
function GetMaxPlayerLevel() return 60 end
function GetGuildInfo() return nil end
function UnitName() return "Pattes" end
function GetRealmName() return "Test Realm" end
function UnitFactionGroup() return "Alliance" end
function UnitSex() return 2 end
function UnitXP() return S.xp end
function UnitXPMax() return S.xpMax end
function GetXPExhaustion() return 300 end
function UnitHealth() return S.hp end
function UnitHealthMax() return S.hpMax end
CurveConstants = { ScaleTo100 = {} }
function UnitPowerPercent() return SECRET end
function UnitPowerType() return 0, "MANA" end
function UnitPower() return S.mana end
function UnitPowerMax() return S.manaMax end
function GetMoney() return 1234 end
function UnitIsDeadOrGhost() return false end
function UnitIsGhost() return false end
function IsResting() return false end
function UnitAffectingCombat() return true end
function GetRealZoneText() return S.zone end
function UnitHealthPercent(u)
	if u == "target" then return T and T.pct end
	return 73.4
end
function GetSubZoneText() return S.subzone end

-- Equipped items by slot; slot 16's item level isn't cached until later.
local gear = {
	[1] = { 10019, "Dreadmist Mask", 3, 30 }, [5] = { 2105, "Thug Shirt — Ünïcode", 1, 5 },
	[7] = { 1366, "Ragged Leather Pants", 0, 2 }, [16] = { 2361, "Battleworn Hammer", 1, nil },
}
local cached = false
local function link(slot)
	local g = gear[slot]
	return g and ("|cff1eff00|Hitem:" .. g[1] .. "::::::::3:::::|h[" .. g[2] .. "]|h|r")
end
function GetInventoryItemLink(_, slot) return link(slot) end

-- Target (T, declared at the top): a rare-elite creature that dies, then an enemy player.
function UnitExists(u) return u == "target" and T ~= nil end
function UnitCanAttack() return T ~= nil end
function UnitIsPlayer(u) return u == "target" and T and T.player or false end
function UnitIsDead(u) return u == "target" and T and T.dead or false end
function UnitClassification() return T and T.rank or "normal" end
function UnitGUID(u) return u == "target" and T and T.guid or "Player-1-00000001" end
function GetInventoryItemID(_, slot) return gear[slot] and gear[slot][1] end
function GetInventoryItemQuality(_, slot) return gear[slot] and gear[slot][3] end
C_Item = { GetDetailedItemLevelInfo = function(l)
	for slot, g in pairs(gear) do
		if link(slot) == l then return (g[4] or (cached and 7)) or nil end
	end
end }

local function load(file) assert(loadfile("addon/ForeverTracker/" .. file))("ForeverTracker") end
load("Core.lua")
load("Quests.lua")
load("Gear.lua")
load("Target.lua")
-- Target-aware unit functions (the addon captured nothing at load time).
local baseName, baseLevel, baseClass = UnitName, UnitLevel, UnitClass
UnitName = function(u) if u == "target" then return T and T.name end return baseName(u) end
UnitLevel = function(u) if u == "target" then return T and T.level end return baseLevel(u) end
UnitClass = function(u) if u == "target" then return T and T.class end return baseClass(u) end
load("Strip.lua")
local FT = ForeverTracker
local eventFrame = frames[1]
local function fire(e, ...) eventFrame.scripts.OnEvent(eventFrame, e, ...) end

fire("ADDON_LOADED", "ForeverTracker")
fire("PLAYER_ENTERING_WORLD", true, false)
for _, t in ipairs(tickers) do t() end

local f = assert(io.open(out, "wb"))
for step = 1, 300 do -- 30 seconds at 10 Hz
	gtime = gtime + 0.1
	if step % 10 == 0 then
		clock = clock + 1
		S.x = S.x + 0.001
		for _, t in ipairs(tickers) do t() end
	end
	if step == 50 then fire("QUEST_TURNED_IN", 233, 450, 120) end
	if step == 120 then cached = true; fire("GET_ITEM_INFO_RECEIVED", 2361, true) end
	if step == 60 then T = { name = "Vicious Grell", level = 9, rank = "rareelite", pct = 100, guid = "Creature-0-1-1-1-2005-0000AB" } end
	if T and not T.player and step > 60 and step < 140 then T.pct = math.max(0, 100 - (step - 60)) end
	if step == 140 then T.dead = true end
	if step == 160 then T = { player = true, name = "Stelfi", class = "Druid", level = 12, guid = "Player-1-00BEEF" } end
	if step == 260 then T = nil end
	if step == 200 then gear[17] = { 2129, "Large Round Shield", 2, 7 }; fire("PLAYER_EQUIPMENT_CHANGED", 17, false) end
	local bytes = FT.StripEncode()
	for i = 0, 170 do f:write(string.char(bytes[i])) end
end
f:close()
print("frames written")
