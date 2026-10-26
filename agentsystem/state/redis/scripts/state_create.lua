local existing = redis.call('GET', KEYS[1])
if not existing then
    redis.call('SET', KEYS[1], ARGV[1])
    return 1
end
if existing == ARGV[1] then
    return 0
end
return -1
