if redis.call('EXISTS', KEYS[3]) == 1 then return -1 end
for index = 1, #ARGV, 2 do
    local existing = redis.call('HGET', KEYS[2], ARGV[index])
    if existing and existing ~= ARGV[index + 1] then
        return ARGV[index]
    end
end

for index = 1, #ARGV, 2 do
    local message_id = ARGV[index]
    if redis.call('HEXISTS', KEYS[2], message_id) == 0 then
        redis.call('HSET', KEYS[2], message_id, ARGV[index + 1])
        redis.call('RPUSH', KEYS[1], message_id)
    end
end

return false
