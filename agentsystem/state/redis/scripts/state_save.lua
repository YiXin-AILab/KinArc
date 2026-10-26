local existing = redis.call('GET', KEYS[1])
if not existing then
    return -2
end
if existing == ARGV[2] then
    return 0
end
if existing ~= ARGV[1] then
    return -1
end
redis.call('SET', KEYS[1], ARGV[2])
return 1
