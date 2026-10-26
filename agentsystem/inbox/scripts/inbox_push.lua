local existing = redis.call('HGET', KEYS[2], ARGV[1])
if existing then
    if existing == ARGV[2] then
        return 0
    end
    local existing_envelope = cjson.decode(existing)
    local candidate_envelope = cjson.decode(ARGV[2])
    local existing_payload = existing_envelope['payload']
    local candidate_payload = candidate_envelope['payload']
    existing_payload['message']['created_at'] = nil
    candidate_payload['message']['created_at'] = nil
    if cjson.encode(existing_payload) == cjson.encode(candidate_payload) then
        return 0
    end
    return -1
end

if redis.call('HLEN', KEYS[2]) >= tonumber(ARGV[3]) then
    return -2
end

redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
redis.call('RPUSH', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4]))
return 1
