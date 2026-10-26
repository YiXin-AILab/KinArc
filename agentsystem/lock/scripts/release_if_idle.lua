if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return -1
end
if redis.call('LLEN', KEYS[2]) > 0 then
    return 0
end
redis.call('DEL', KEYS[1])
return 1
