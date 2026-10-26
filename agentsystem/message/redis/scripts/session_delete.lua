-- Validate types before writes: a Lua command error does not roll back.
local expected = {'hash', 'zset', 'zset'}
for index, key in ipairs(KEYS) do
    local kind = redis.call('TYPE', key).ok
    if kind ~= 'none' and kind ~= expected[index] then
        return redis.error_reply('invalid session catalog key type')
    end
end
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return false end
local item = cjson.decode(raw)
item.is_deleted = true
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(item))
redis.call('ZREM', KEYS[2], ARGV[2])
redis.call('ZREM', KEYS[3], ARGV[2])
return true
