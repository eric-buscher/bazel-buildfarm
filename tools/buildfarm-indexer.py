from redis.client import Pipeline
from rediscluster import StrictRedisCluster
import sys

def get_cas_page(r, cursor, count):
    return r.scan(
        cursor=cursor,
        match="ContentAddressableStorage:*",
        count=count)

redis_host = None
if len(sys.argv) > 1:
    redis_host = sys.argv[1]
if not redis_host:
    print ("usage: buildfarm-indexer.py <redis_host>")
    sys.exit(1)

r = StrictRedisCluster(startup_nodes=[{"host": redis_host, "port": 6379}], skip_full_coverage_check=True)

nodes = r.connection_pool.nodes

slots = set(range(0, 16384))

node_key = 0
node_keys = {}
while slots:
    node_key = node_key + 1
    slot = nodes.keyslot(str(node_key))
    if slot in slots:
        slots.remove(slot)
        node_keys[slot] = str(node_key)

workers = r.hkeys("Workers")

worker_count = len(workers)

print ("%d workers" % worker_count)

p = r.pipeline()
for node_key in node_keys.viewvalues():
    p.delete("{%s}:intersecting-workers" % node_key)
    p.sadd("{%s}:intersecting-workers" % node_key, *workers)
p.execute()

print ("created sets")

oversized_cas_names = []

def map_cas_page(r, count, method):
    cursors = {}
    conns = {}
    for master_node in r.connection_pool.nodes.all_masters():
        cursors[master_node["name"]] = "0"
        conns[master_node["name"]] = r.connection_pool.get_connection_by_node(master_node)

    while not all(cursors[node] == 0 for node in cursors):
        for node in cursors:
            if cursors[node] == 0:
                continue

            conn = conns[node]

            pieces = [
                'SCAN', cursors[node],
                'MATCH', "ContentAddressableStorage:*"
            ]
            if count is not None:
                pieces.extend(['COUNT', count])

            conn.send_command(*pieces)

            raw_resp = conn.read_response()

            # if you don't release the connection, the driver will make another, and you will hate your life
            cur, resp = r._parse_scan(raw_resp)

            if method(resp, conn):
                cursors[node] = cur
    for conn in conns.values():
        r.connection_pool.release(conn)

class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def get_connection(self, command, hint):
        return self.connection

    def release(self, conn):
        pass

class Indexer:
    def __init__(self, r):
        self.processed = 0
        self.r = r

    def pipeline(self, conn):
        return Pipeline(connection_pool=FakePool(conn), response_callbacks={}, transaction=False, shard_hint=None)

    def process(self, cas_names, conn):
        count = len(cas_names)
        p = self.pipeline(conn)
        for i in range(count):
            name = cas_names[i]
            node_key = node_keys[nodes.keyslot(str(name))]
            set_key = "{%s}:intersecting-workers" % node_key
            p.sinterstore(name, set_key, name)
        p.execute()
        self.processed += count
        sys.stdout.write("Page Complete: %d %d total\r" % (count, self.processed))
        sys.stdout.flush()
        return True

indexer = Indexer(r)

map_cas_page(r, 10000, indexer.process)

p = r.pipeline()
for node_key in node_keys.viewvalues():
    p.delete("{%s}:intersecting-workers" % node_key)
p.execute()

print("\n%d processed" % (indexer.processed))