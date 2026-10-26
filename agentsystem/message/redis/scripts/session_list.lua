local members = redis.call('ZRANGE', KEYS[2], ARGV[1], '-', 'BYLEX', 'REV', 'LIMIT', 0, ARGV[2])
local result = {}
for _, member in ipairs(members) do
    local identity = string.sub(member, -64)
    local payload = redis.call('HGET', KEYS[1], identity)
    if not payload then return redis.error_reply('session index missing record') end
    table.insert(result, member)
    table.insert(result, payload)
end
return result
