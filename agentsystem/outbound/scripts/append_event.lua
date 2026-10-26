-- 两个 key 必须处于同一 hash slot；追加、世代更新、裁剪和续期不可被读取插入。
-- 返回分配的 Stream ID、有效 epoch、生命周期及近似裁剪后的长度。
local stream_key = KEYS[1]
local metadata_key = KEYS[2]
local candidate_epoch = ARGV[1]
local event_json = ARGV[2]
local event_type = ARGV[3]
local now_ms = tonumber(ARGV[4])
local retention_seconds = tonumber(ARGV[5])
local terminal_grace_seconds = tonumber(ARGV[6])
local max_events = tonumber(ARGV[7])

local epoch = redis.call('HGET', metadata_key, 'epoch')
local lifecycle = redis.call('HGET', metadata_key, 'lifecycle')
if not epoch then
    -- metadata 丢失意味着旧游标已失效，移除残留正文以免混入新世代。
    redis.call('DEL', stream_key)
    epoch = candidate_epoch
    lifecycle = 'open'
end

if event_type == 'run.started' then
    lifecycle = 'open'
elseif event_type == 'run.completed'
    or event_type == 'run.failed'
    or event_type == 'run.canceled' then
    lifecycle = 'terminal'
end

local stream_id = redis.call(
    'XADD', stream_key, '*', 'event', event_json, 'type', event_type
)
local cutoff_ms = math.max(0, now_ms - (retention_seconds * 1000))
redis.call('XTRIM', stream_key, 'MINID', '~', tostring(cutoff_ms) .. '-0')
redis.call('XTRIM', stream_key, 'MAXLEN', '~', max_events)
redis.call(
    'HSET', metadata_key, 'epoch', epoch, 'lifecycle', lifecycle
)

local ttl = retention_seconds
if lifecycle == 'terminal' then
    ttl = terminal_grace_seconds
end
redis.call('EXPIRE', stream_key, ttl)
redis.call('EXPIRE', metadata_key, ttl)

return {stream_id, epoch, lifecycle, redis.call('XLEN', stream_key)}
