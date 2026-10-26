if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then
    return 0
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
if ARGV[3] == '1' then
    redis.call('ZADD', KEYS[2], 0, ARGV[1])
end
return 1
