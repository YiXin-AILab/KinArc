if redis.call('EXISTS', KEYS[4]) == 1 then return -1 end
local expected = ARGV[1]
local message_id = ARGV[2]
local payload = ARGV[3]
local current = redis.call('GET', KEYS[3])
local existing = redis.call('HGET', KEYS[2], message_id)

if existing and existing ~= payload then
    return message_id
end

if current == message_id and existing == payload then
    return false
end

local normalized_current = current or ''
if normalized_current ~= expected then
    return current or '__none__'
end

if not existing then
    redis.call('HSET', KEYS[2], message_id, payload)
    redis.call('RPUSH', KEYS[1], message_id)
end
redis.call('SET', KEYS[3], message_id)
return false
