-- The record and both listing indexes share one user hash slot.
-- Lua is isolated, but command errors do not roll back earlier writes.
local expected = {'hash', 'zset', 'zset'}
for index, key in ipairs(KEYS) do
    local kind = redis.call('TYPE', key).ok
    if kind ~= 'none' and kind ~= expected[index] then
        return redis.error_reply('invalid session catalog key type')
    end
end
local existing = redis.call('HGET', KEYS[1], ARGV[1])
if existing then return existing end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('ZADD', KEYS[2], 0, ARGV[3])
redis.call('ZADD', KEYS[3], 0, ARGV[3])
return ARGV[2]
