local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return false end
local item = cjson.decode(raw)
if item.is_deleted then return false end
local ctx = item.ctx
if ctx.app_id ~= ARGV[5] or ctx.user_id ~= ARGV[6]
    or ctx.agent_id ~= ARGV[7] or ctx.session_id ~= ARGV[8] then
    return {-2}
end
if ARGV[4] ~= '' and item.title_version ~= tonumber(ARGV[4]) then
    return {-1}
end
if item.title == ARGV[2] then return {raw} end
item.title = ARGV[2]
item.title_version = item.title_version + 1
if ARGV[3] > item.updated_at then item.updated_at = ARGV[3] end
local current = cjson.encode(item)
redis.call('HSET', KEYS[1], ARGV[1], current)
return {current}
