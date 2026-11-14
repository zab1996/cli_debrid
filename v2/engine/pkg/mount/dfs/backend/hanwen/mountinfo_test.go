package hanwen

import "testing"

func TestUnescapeMountinfo(t *testing.T) {
	if got := unescapeMountinfo(`/home/ryan/usenet\040stream\040cache`); got != "/home/ryan/usenet stream cache" {
		t.Fatalf("got %q", got)
	}
	if got := unescapeMountinfo("/plain"); got != "/plain" {
		t.Fatalf("got %q", got)
	}
}

func TestIsFuseMountPointPlainDir(t *testing.T) {
	if isFuseMountPoint(t.TempDir()) {
		t.Fatal("a plain directory is not a FUSE mount")
	}
	if isFuseMountPoint("/") {
		t.Fatal("/ is not a FUSE mount")
	}
}
