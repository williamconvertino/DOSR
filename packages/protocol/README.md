# DOSR Protocol Package

Keep all wire types here and version them explicitly. Use deterministic serialization before hashing/signing. For JSON, use RFC 8785 JSON Canonicalization Scheme (JCS), or replace JSON signatures entirely with a typed binary encoding later.

Git object IDs should be algorithm-qualified strings (for example `sha1:<hex>`), so the protocol does not silently assume SHA-1 forever.
