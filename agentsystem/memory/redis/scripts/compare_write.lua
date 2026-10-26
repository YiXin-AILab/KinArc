local current = redis.call('GET', KEYS[1]) or ''
if current ~= ARGV[1] then return 0 end
if ARGV[2] == 'write' then redis.call('SET', KEYS[1], ARGV[3]) end
return 1
