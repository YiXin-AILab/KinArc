if #ARGV < 4 then
    return {}
end

local owner_token = ARGV[1]
local lease_ms = tonumber(ARGV[2])
local inbox_ttl_seconds = tonumber(ARGV[3])
local selected_modes = {}
for index = 4, #ARGV do
    selected_modes[ARGV[index]] = true
end

local redis_time = redis.call('TIME')
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)
local expires_at_ms = now_ms + lease_ms
local ids = redis.call('LRANGE', KEYS[1], 0, -1)
local selected = {}

for _, message_id in ipairs(ids) do
    local payload = redis.call('HGET', KEYS[2], message_id)
    if payload then
        local envelope = cjson.decode(payload)
        local mode = envelope['payload']['message']['mode']
        if selected_modes[mode] then
            local claim = redis.call('HGET', KEYS[3], message_id)
            local can_claim = claim == false
            if claim then
                local separator = string.find(claim, '|', 1, true)
                local claim_owner = string.sub(claim, 1, separator - 1)
                local claim_expiry = tonumber(string.sub(claim, separator + 1))
                can_claim = claim_owner == owner_token or claim_expiry <= now_ms
            end
            if can_claim then
                redis.call('HSET', KEYS[3], message_id, owner_token .. '|' .. expires_at_ms)
                table.insert(selected, payload)
            end
        end
    end
end

redis.call('EXPIRE', KEYS[1], inbox_ttl_seconds)
redis.call('EXPIRE', KEYS[2], inbox_ttl_seconds)
redis.call('EXPIRE', KEYS[3], inbox_ttl_seconds)
return selected
