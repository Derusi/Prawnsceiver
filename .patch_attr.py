import io

def load(path):
    return io.open(path, newline='', encoding='utf-8').read().splitlines(keepends=True)

def save(path, lines):
    io.open(path, 'w', newline='', encoding='utf-8').write(''.join(lines))

def find_one(lines, needle, start=0):
    hits = [i for i, l in enumerate(lines) if needle in l and i >= start]
    assert len(hits) == 1, (needle, hits)
    return hits[0]

p = 'noaa_receiver/decoding/ais.py'
lines = load(p)

# 1. tag the thread's channel states with the dongle id
i = find_one(lines, 'st = [new_channel_state() for _ in shifts]')
lines[i + 1:i + 1] = [
    '    # Dual-AIS support: two dongles may listen at the same time (A/B\n',
    '    # comparison); every decoded frame is stamped with the receiving\n'
    '    # dongle in the persistent log, so attribution is unambiguous\n',
    '    for s_ in st:\n',
    '        s_["dongle"] = did\n',
]
save(p, lines)

# 2. stamp the log entry with the receiving dongle
lines = load(p)
i = find_one(lines, 'entry = build_log_entry(d, channel, sentences, now)')
lines[i + 1:i + 1] = [
    '            if ch_st.get("dongle"):\n',
    '                entry["dng"] = ch_st["dongle"]\n',
]
save(p, lines)
import ast
ast.parse(''.join(load(p)))
print('ais.py attribution added')

# 3. test_ais: assert the stamp lands in the log
t = 'tests/test_ais.py'
lines = load(t)
i = find_one(lines, 'ais.handle_frames([t5], "B", ais.new_channel_state())')
lines[i:i + 1] = ['ch12 = ais.new_channel_state()\r\n',
                  'ch12["dongle"] = "test-dongle:1"\r\n',
                  'ais.handle_frames([t5], "B", ch12)\r\n']
save(t, lines)
lines = load(t)
i = find_one(lines, 'assert log[0]["mmsi"] == 230985000 and log[0]["name"] == "AILA", log')
lines[i + 1:i + 1] = ['assert log[0]["dng"] == "test-dongle:1", log[0]\r\n']
save(t, lines)
ast.parse(''.join(load(t)))
print('test_ais attribution check added')
