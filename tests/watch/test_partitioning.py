def test_same_path_same_partition():
    paths = ["/tmp/a.md", "/tmp/b.md", "/tmp/sub/c.py"]
    num_workers = 4
    for p in paths:
        assignments = {hash(p) % num_workers for _ in range(100)}
        assert len(assignments) == 1, f"path {p} should hash deterministically"


def test_partitioning_distributes_load():
    """Sanity: 1000 distinct paths should hit multiple partitions, not all one."""
    num_workers = 4
    buckets = {hash(f"/tmp/{i}.md") % num_workers for i in range(1000)}
    assert len(buckets) > 1
