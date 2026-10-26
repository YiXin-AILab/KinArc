local current = redis.call('HGET', KEYS[1], ARGV[1])
if not current then
    return -1
end
if current ~= ARGV[2] then
    return 0
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
if ARGV[4] == '1' then
    redis.call('ZADD', KEYS[2], 0, ARGV[1])
else
    redis.call('ZREM', KEYS[2], ARGV[1])
end
return 1
