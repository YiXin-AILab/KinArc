if #ARGV < 2 then
    return 0
end

local owner_token = ARGV[1]
local completed = {}
for index = 2, #ARGV do
    local message_id = ARGV[index]
    if redis.call('HEXISTS', KEYS[2], message_id) == 1 then
        local claim = redis.call('HGET', KEYS[3], message_id)
        if not claim then
            return -1
        end
        local separator = string.find(claim, '|', 1, true)
        local claim_owner = string.sub(claim, 1, separator - 1)
        if claim_owner ~= owner_token then
            return -1
        end
        completed[message_id] = true
    end
end

for message_id, _ in pairs(completed) do
    redis.call('HDEL', KEYS[2], message_id)
    redis.call('HDEL', KEYS[3], message_id)
end

local ids = redis.call('LRANGE', KEYS[1], 0, -1)
local retained = {}
local order_ttl = redis.call('PTTL', KEYS[1])
for _, message_id in ipairs(ids) do
    if not completed[message_id] and redis.call('HEXISTS', KEYS[2], message_id) == 1 then
        table.insert(retained, message_id)
    end
end

redis.call('DEL', KEYS[1])
if #retained > 0 then
    redis.call('RPUSH', KEYS[1], unpack(retained))
    if order_ttl > 0 then
        redis.call('PEXPIRE', KEYS[1], order_ttl)
    end
end
return 1
