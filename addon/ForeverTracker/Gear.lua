-- Equipped gear: what the player is wearing, for the website's equipment card.
-- Each item: { slot = inventory slot id, id = item id, name, quality, ilvl }.

local FT = ForeverTracker
local safe, read = FT.Safe, FT.Read
local handlers = FT.handlers

-- Inventory slot ids, in character-pane order. 4 = shirt, 19 = tabard.
local SLOTS = { 1, 2, 3, 15, 5, 4, 19, 9, 10, 6, 7, 8, 11, 12, 13, 14, 16, 17, 18 }

local function itemLevel(link)
	if C_Item and C_Item.GetDetailedItemLevelInfo then
		local v = read(nil, C_Item.GetDetailedItemLevelInfo, link)
		if v then return v end
	end
	if GetDetailedItemLevelInfo then
		local v = read(nil, GetDetailedItemLevelInfo, link)
		if v then return v end
	end
	if GetItemInfo then
		local ok, _, _, _, level = pcall(GetItemInfo, link)
		if ok then return safe(level) end
	end
end

-- Returns the item list and whether some item info wasn't cached yet.
function FT.ReadGear()
	local items, missing = {}, false
	for _, slot in ipairs(SLOTS) do
		local link = read(nil, GetInventoryItemLink, "player", slot)
		if link then
			local id = read(nil, GetInventoryItemID, "player", slot) or tonumber(link:match("item:(%d+)"))
			local quality = read(nil, GetInventoryItemQuality, "player", slot)
			local ilvl = itemLevel(link)
			if not ilvl then missing = true end
			items[#items + 1] = {
				slot = slot,
				id = id,
				name = link:match("%[(.-)%]"),
				quality = quality,
				ilvl = ilvl,
			}
		end
	end
	return items, missing
end

function handlers.PLAYER_EQUIPMENT_CHANGED()
	FT.gearDirty = true
end

function handlers.UNIT_INVENTORY_CHANGED(unit)
	if safe(unit) == "player" then FT.gearDirty = true end
end

-- Item levels of uncached items arrive later.
function handlers.GET_ITEM_INFO_RECEIVED()
	if FT.gearIncomplete then FT.gearDirty = true end
end
