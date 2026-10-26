local count = tonumber(ARGV[1])
for index = 0, count - 1 do
    local offset = 2 + index * 3
    local current = redis.call('HGET', KEYS[1], ARGV[offset])
    if not current or current ~= ARGV[offset + 1] then
        return 0
    end
end
for index = 0, count - 1 do
    local offset = 2 + index * 3
    redis.call('HSET', KEYS[1], ARGV[offset], ARGV[offset + 2])
end
return 1
