-- Live data strip: a row of coloured cells in the top-left corner of the
-- screen. Each cell carries 3 bytes (R, G, B). The companion app reads it
-- with screen capture. Layout (171 bytes = 57 cells of CELL_PX x CELL_PX):
--
--   0-1   magic "FT"            2   protocol version
--   3     frame counter         4   flags (see FLAG_*)
--   5-6   valid mask            7   level
--   8     power type id         9-11 xp        12-14 xpMax
--   15-17 rested               18-20 health    21-23 healthMax
--   24-26 power                27-29 powerMax  30-32 uiMapID
--   33-34 x * 65535            35-36 y * 65535 37-40 money (copper)
--   41-44 server time          45   message id 46 chunk index
--   47    chunk count          48   chunk length
--   49-165 chunk bytes         166  character tag (FT.CharTag; 0 = unknown)
--   167   health %             168  power %
--   169-170 Fletcher-16 of bytes 0-168
--
-- Messages (identity, zone, quest log, gear, professions, jumps, target, journal events) are JSON prefixed with
-- a type letter, split into chunks and shown one chunk per HOLD interval.
-- Unreadable (secret) values have their valid bit cleared and are sent as 0.

local FT = ForeverTracker

local VERSION = 3
local CELL_PX = 4
local NUM_BYTES = 171
local CHUNK = 117
local UPDATE = 0.1     -- seconds between strip redraws
local HOLD = 2         -- redraws each chunk stays on screen

local FLAG_SHARING, FLAG_DEAD, FLAG_GHOST, FLAG_COMBAT, FLAG_RESTING, FLAG_HIDDEN, FLAG_LEAVING, FLAG_COMBATLOG =
	1, 2, 4, 8, 16, 32, 64, 128
local VALID = { level = 1, xp = 2, health = 4, healthMax = 8, power = 16, powerMax = 32, pos = 64, money = 128,
	map = 256, healthPct = 512, powerPct = 1024 }

-- JSON ----------------------------------------------------------------------

local function isArray(t)
	local n = 0
	for _ in pairs(t) do n = n + 1 end
	return n == #t
end

local escapes = { ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t" }
local function json(v)
	local t = type(v)
	if t == "string" then
		return '"' .. v:gsub('[%c"\\]', function(c)
			return escapes[c] or string.format("\\u%04x", c:byte())
		end) .. '"'
	elseif t == "number" then
		if v ~= v or v == math.huge or v == -math.huge then return "null" end
		return v == math.floor(v) and string.format("%d", v) or string.format("%.6g", v)
	elseif t == "boolean" then
		return tostring(v)
	elseif t == "table" then
		local parts = {}
		if next(v) ~= nil and isArray(v) then
			for _, item in ipairs(v) do parts[#parts + 1] = json(item) end
			return "[" .. table.concat(parts, ",") .. "]"
		end
		for k, item in pairs(v) do
			parts[#parts + 1] = json(tostring(k)) .. ":" .. json(item)
		end
		return "{" .. table.concat(parts, ",") .. "}"
	end
	return "null"
end
FT.JSON = json

-- Message queue ---------------------------------------------------------------

local queue = {}       -- pending payload strings, sent in order
local current          -- { id, data, index, count, shown }
local nextID = 0
local lastSent = {}    -- type -> time last queued
local lastQuests
local lastGear
local lastTarget
local lastProfessions, lastJumps

local function enqueue(kind, payload)
	local data = kind .. json(payload)
	-- Replace a not-yet-started message of the same kind instead of piling up.
	for i, item in ipairs(queue) do
		if item.kind == kind and kind ~= "E" then
			queue[i] = { kind = kind, data = data }
			return
		end
	end
	queue[#queue + 1] = { kind = kind, data = data, t = kind == "E" and payload.t or nil }
	lastSent[kind] = GetTime()
end

-- Drops journal events from `since` on that are still waiting to be sent
-- (offline time the player chose to keep private).
function FT.DropQueuedEvents(since)
	for i = #queue, 1, -1 do
		if queue[i].kind == "E" and queue[i].t and queue[i].t >= since then table.remove(queue, i) end
	end
end

local function identity()
	local s = FT.current
	if not s then return nil end
	return { name = s.name, realm = s.realm, faction = s.faction, race = s.race, class = s.class,
		classToken = s.classToken, sex = s.sex, guild = s.guild, maxLevel = s.maxLevel,
		guid = FT.Read(nil, UnitGUID, "player") }
end

local lastZoneKey
local function schedule()
	local now = GetTime()
	local s = FT.current
	if not s then return end
	if not lastSent.I or now - lastSent.I > 30 then
		enqueue("I", identity())
	end
	local zoneKey = tostring(s.zone) .. "|" .. tostring(s.subzone) .. "|" .. tostring(s.map)
	if zoneKey ~= lastZoneKey or not lastSent.Z or now - lastSent.Z > 15 then
		lastZoneKey = zoneKey
		enqueue("Z", { zone = s.zone, subzone = s.subzone, map = s.map })
	end
	if FT.db.quests and (FT.questsVersion ~= lastQuests or not lastSent.Q or now - lastSent.Q > 60) then
		lastQuests = FT.questsVersion
		enqueue("Q", FT.db.quests)
	end
	if FT.db.gear and (FT.gearVersion ~= lastGear or not lastSent.G or now - lastSent.G > 120) then
		lastGear = FT.gearVersion
		enqueue("G", FT.db.gear)
	end
	-- Professions as compact rows: [name, rank, max, bonus, secondary 0/1].
	if FT.db.professions and (FT.professionsVersion ~= lastProfessions or not lastSent.P or now - lastSent.P > 120) then
		lastProfessions = FT.professionsVersion
		local rows = {}
		for i, p in ipairs(FT.db.professions) do
			rows[i] = { p.name, p.rank or 0, p.max or 0, p.bonus or 0, p.secondary and 1 or 0 }
		end
		enqueue("P", rows)
	end
	-- Jumps: within 2 s of a change, and every minute.
	local jumps = FT.Jumps and FT.Jumps() or 0
	local sinceJ = lastSent.J and now - lastSent.J or math.huge
	if jumps > 0 and ((jumps ~= lastJumps and sinceJ >= 2) or sinceJ > 60) then
		lastJumps = jumps
		enqueue("J", { n = jumps })
	end
	-- Target: on change (at most twice a second) and every 15 s; {} means none.
	local target = FT.ReadTarget and FT.ReadTarget()
	local targetKey = target and json(target) or ""
	local sinceT = lastSent.T and now - lastSent.T or math.huge
	if (targetKey ~= lastTarget and sinceT >= 0.5) or sinceT > 15 then
		lastTarget = targetKey
		enqueue("T", target or {})
	end
end

FT.OnEvent(function(e) enqueue("E", e) end)

-- Sharing again: send every message kind right away instead of on its timer.
FT.OnResume(function()
	wipe(lastSent)
	lastQuests, lastGear, lastProfessions, lastJumps, lastTarget, lastZoneKey = nil, nil, nil, nil, nil, nil
end)

local function nextChunk()
	if current and current.index < current.count then
		current.index = current.index + 1
		current.shown = 0
		return
	end
	local item = table.remove(queue, 1)
	if not item then
		current = nil
		return
	end
	nextID = nextID % 255 + 1
	current = {
		id = nextID, data = item.data, index = 1, shown = 0,
		count = math.max(1, math.ceil(#item.data / CHUNK)),
	}
end

-- Encoding --------------------------------------------------------------------

local bytes = {}

local function put(offset, value, width)
	value = math.max(0, math.floor(value or 0))
	value = math.min(value, 256 ^ width - 1)
	for i = width - 1, 0, -1 do
		bytes[offset + i] = value % 256
		value = math.floor(value / 256)
	end
end

local counter = 0
local function encode()
	for i = 0, NUM_BYTES - 1 do bytes[i] = 0 end
	bytes[0], bytes[1], bytes[2] = 70, 84, VERSION -- "FT"
	counter = (counter + 1) % 256
	bytes[3] = counter

	local settings = FT.db.settings
	local flags, valid = 0, 0
	if settings.appearOffline or FT.db.offlineSince then
		flags = FLAG_HIDDEN
	else
		local s = FT.current and FT.BuildSnapshot()
		if s then
			flags = FLAG_SHARING
			if s.dead then flags = flags + FLAG_DEAD end
			if s.ghost then flags = flags + FLAG_GHOST end
			if s.combat then flags = flags + FLAG_COMBAT end
			if s.resting then flags = flags + FLAG_RESTING end
			if s.status == "offline" then flags = flags + FLAG_LEAVING end
			if settings.combatLog then flags = flags + FLAG_COMBATLOG end
			local p = s.power or {}
			local fields = {
				{ "level", s.level, 7, 1 }, { "xp", s.xp and s.xpMax and s.xp, 9, 3 },
				{ "health", s.health, 18, 3 }, { "healthMax", s.healthMax, 21, 3 },
				{ "power", p.cur, 24, 3 }, { "powerMax", p.max, 27, 3 }, { "money", s.money, 37, 4 },
				{ "healthPct", s.healthPct, 167, 1 }, { "powerPct", p.pct, 168, 1 },
			}
			for _, f in ipairs(fields) do
				if f[2] then
					valid = valid + VALID[f[1]]
					put(f[3], f[2], f[4])
				end
			end
			if s.xp and s.xpMax then
				put(12, s.xpMax, 3)
				put(15, s.rested, 3)
			end
			put(8, p.id, 1)
			if s.mapID then
				valid = valid + VALID.map
				put(30, s.mapID, 3)
			end
			if s.x and s.y then
				valid = valid + VALID.pos
				put(33, s.x * 65535, 2)
				put(35, s.y * 65535, 2)
			end
			put(41, FT.Now(), 4)
			bytes[166] = FT.CharTag(s.name, s.realm)

			schedule()
			if not current or current.shown >= HOLD then nextChunk() end
		end
	end
	bytes[4] = flags
	put(5, valid, 2)

	if current then
		current.shown = current.shown + 1
		local from = (current.index - 1) * CHUNK + 1
		local chunk = current.data:sub(from, from + CHUNK - 1)
		bytes[45], bytes[46], bytes[47], bytes[48] = current.id, current.index, current.count, #chunk
		for i = 1, #chunk do bytes[48 + i] = chunk:byte(i) end
	end

	local a, b = 0, 0
	for i = 0, NUM_BYTES - 3 do
		a = (a + bytes[i]) % 255
		b = (b + a) % 255
	end
	bytes[NUM_BYTES - 2], bytes[NUM_BYTES - 1] = b, a
end

-- Drawing ---------------------------------------------------------------------

local strip
local cells = {}
local NUM_CELLS = NUM_BYTES / 3

-- Sizes cells to exactly CELL_PX physical pixels.
local function layout()
	local _, physH = GetPhysicalScreenSize()
	local size = CELL_PX * 768 / physH -- UI units per physical pixel at scale 1
	strip:SetSize(NUM_CELLS * size, size)
	for i = 0, NUM_CELLS - 1 do
		cells[i]:SetSize(size, size)
		cells[i]:SetPoint("TOPLEFT", strip, "TOPLEFT", i * size, 0)
	end
end

local function build()
	-- No parent: unaffected by UI scale and still shown when the UI is hidden (Alt+Z).
	strip = CreateFrame("Frame", "ForeverTrackerStrip")
	strip:SetFrameStrata("TOOLTIP")
	strip:SetFrameLevel(10000)
	strip:SetScale(1)
	strip:SetPoint("TOPLEFT", nil, "TOPLEFT", 0, 0)
	for i = 0, NUM_CELLS - 1 do
		local tex = strip:CreateTexture(nil, "OVERLAY")
		if tex.SetSnapToPixelGrid then
			tex:SetSnapToPixelGrid(false)
			tex:SetTexelSnappingBias(0)
		end
		cells[i] = tex
	end
	layout()
end

local function draw()
	encode()
	for i, tex in pairs(cells) do
		tex:SetColorTexture(bytes[i * 3] / 255, bytes[i * 3 + 1] / 255, bytes[i * 3 + 2] / 255, 1)
	end
end

local elapsed = 0
local function onUpdate(_, dt)
	elapsed = elapsed + dt
	if elapsed < UPDATE then return end
	elapsed = 0
	draw()
end

local function apply()
	if not strip then return end
	strip:SetShown(FT.db.settings.enabled)
end

FT.OnLoaded(function()
	build()
	strip:SetScript("OnUpdate", onUpdate)
	apply()
end)
FT.OnSettingsChanged(apply)

FT.handlers.DISPLAY_SIZE_CHANGED = function()
	if strip then layout() end
end

-- For tests.
FT.StripEncode = function() encode(); return bytes end
