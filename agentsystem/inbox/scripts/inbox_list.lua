local ids = redis.call('LRANGE', KEYS[1], 0, -1)
local pending = {}
for _, message_id in ipairs(ids) do
    local payload = redis.call('HGET', KEYS[2], message_id)
    if payload then
        table.insert(pending, payload)
    end
end
return pending
