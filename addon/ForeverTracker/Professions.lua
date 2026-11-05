-- Professions and secondary skills, for the website's professions card, plus
-- journal entries when one is learned, every 5 points and at the cap.
-- Ported from upstream (patrick123900/wow-tracker, Character.lua).

local FT = ForeverTracker
local safe, read = FT.Safe, FT.Read
local handlers = FT.handlers

local PRIMARY_HEADERS = { [TRADE_SKILLS or "Professions"] = true, Professions = true }
local SECONDARY_HEADERS = { [(SECONDARY_SKILLS or "Secondary Skills"):gsub(":$", "")] = true, ["Secondary Skills"] = true }
-- Recognised by name too, in case a client names the sections differently.
local KNOWN = {
	Alchemy = "primary", Blacksmithing = "primary", Enchanting = "primary", Engineering = "primary",
	Herbalism = "primary", Leatherworking = "primary", Mining = "primary", Skinning = "primary",
	Tailoring = "primary", Jewelcrafting = "primary", Inscription = "primary",
	Cooking = "secondary", ["First Aid"] = "secondary", Fishing = "secondary",
}

local function entry(name, rank, max, bonus, kind)
	bonus = safe(bonus)
	return { name = name, rank = safe(rank), max = safe(max), bonus = bonus and bonus ~= 0 and bonus or nil,
		secondary = kind == "secondary" or nil }
end

-- Modern clients: GetProfessions() gives indices for GetProfessionInfo.
local function fromProfessionAPI()
	if not (GetProfessions and GetProfessionInfo) then return nil end
	local ok, p1, p2, arch, fish, cook, aid = pcall(GetProfessions)
	if not ok then return nil end
	local list = {}
	for i, index in pairs({ p1, p2, arch, fish, cook, aid }) do
		index = safe(index)
		if index then
			local ok2, name, _, rank, max, _, _, _, modifier = pcall(GetProfessionInfo, index)
			name = ok2 and safe(name)
			if name then list[#list + 1] = entry(name, rank, max, modifier, i <= 2 and "primary" or "secondary") end
		end
	end
	return list
end

-- Classic clients: the skills pane. Returns nil when it can't be read
-- completely right now (a collapsed section hides its entries).
local function fromSkillLines()
	if not (GetNumSkillLines and GetSkillLineInfo) then return nil end
	local n = read(nil, GetNumSkillLines)
	if not n or n == 0 then return nil end
	local list, kind = {}, nil
	for i = 1, n do
		local ok, name, isHeader, isExpanded, rank, _, modifier, maxRank = pcall(GetSkillLineInfo, i)
		name = ok and safe(name)
		if name then
			if isHeader then
				kind = PRIMARY_HEADERS[name] and "primary" or SECONDARY_HEADERS[name] and "secondary" or nil
				if kind and not isExpanded then return nil end
			elseif kind or KNOWN[name] then
				list[#list + 1] = entry(name, rank, maxRank, modifier, kind or KNOWN[name])
			end
		end
	end
	return list
end

function FT.ReadProfessions()
	local list = fromProfessionAPI()
	if list and #list > 0 then return list end
	return fromSkillLines() or list
end

-- A stable key, so the list is only sent on when something changed.
function FT.ProfessionsKey(list)
	local parts = {}
	for _, p in ipairs(list) do
		parts[#parts + 1] = table.concat({ tostring(p.name), tostring(p.rank), tostring(p.max), tostring(p.bonus) }, ":")
	end
	return table.concat(parts, "|")
end

-- Journal entries: when one is learned, every 5 points and at the current cap
-- (an entry per point would flood the journal).
function FT.RecordSkillUps(old, new)
	local before = {}
	for _, p in ipairs(old or {}) do before[p.name] = p.rank end
	for _, p in ipairs(new) do
		local was = before[p.name]
		if was and p.rank and p.rank > was then
			local milestone = math.floor(p.rank / 5) > math.floor(was / 5) or p.rank == p.max
			if milestone then FT.AddEvent("skill", { skill = p.name, rank = p.rank, max = p.max }) end
		elseif old and not was then
			FT.AddEvent("skill", { skill = p.name, rank = p.rank, max = p.max, learned = true })
		end
	end
end

function FT.ProfessionsDebug()
	local n = GetNumSkillLines and read(nil, GetNumSkillLines)
	local headers = {}
	for i = 1, n or 0 do
		local ok, name, isHeader, isExpanded = pcall(GetSkillLineInfo, i)
		if ok and isHeader then headers[#headers + 1] = tostring(safe(name)) .. (isExpanded and "" or " (collapsed)") end
	end
	local list = FT.ReadProfessions()
	FT.Print(string.format("skills: GetProfessions %s, %s skill lines, sections: %s; professions read: %s",
		GetProfessions and "yes" or "no", tostring(n), table.concat(headers, ", "), list and #list or "unreadable"))
end

function handlers.SKILL_LINES_CHANGED() FT.professionsDirty = true end
function handlers.CHAT_MSG_SKILL() FT.professionsDirty = true end
