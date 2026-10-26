-- This barrier shares the message slot; late commits cannot resurrect history.
redis.call('SET', KEYS[4], '1')
if ARGV[1] == '1' then
    redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
end
return true
