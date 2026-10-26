local existing = redis.call('HGET', KEYS[3], ARGV[3])
if existing then
    return {0, existing}
end
local current = redis.call('HGET', KEYS[1], ARGV[1])
if not current or current ~= ARGV[2] then
    return {-1, ''}
end
if redis.call('HEXISTS', KEYS[2], ARGV[4]) == 1 then
    return {-2, ''}
end
redis.call('HSET', KEYS[2], ARGV[4], ARGV[5])
redis.call('HSET', KEYS[3], ARGV[3], ARGV[4])
if ARGV[6] ~= '' then
    redis.call('HSET', KEYS[1], ARGV[1], ARGV[6])
    redis.call('ZREM', KEYS[4], ARGV[1])
end
return {1, ARGV[4]}
