local current = redis.call('HGET', KEYS[1], ARGV[1])
if not current then
    return -1
end
if current ~= ARGV[2] then
    return 0
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
return 1
