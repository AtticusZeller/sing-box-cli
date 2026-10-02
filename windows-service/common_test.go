package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestInvocationPreservesArguments(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "Test User", "代理配置")
	core := filepath.Join(dir, "Program Files", "sing-box.exe")
	args := []string{"install", "sing-box-service", "--work-dir", dir, "--", core, "run", "-c", filepath.Join(dir, "config.json"), "-D", dir}
	in, err := parseInvocation(args)
	if err != nil {
		t.Fatal(err)
	}
	if in.directory != dir || in.args[0] != core || len(in.args) != 6 || in.args[3] != args[8] {
		t.Fatalf("arguments changed: %#v", in)
	}
}

func TestInvalidInvocation(t *testing.T) {
	for _, args := range [][]string{{}, {"unknown", "service"}, {"stop", "../service"}, {"status", "service", "extra"}, {"install", "service", "--work-dir", ".", "--", "core"}} {
		if _, err := parseInvocation(args); err == nil {
			t.Fatalf("accepted %q", args)
		}
	}
}

func TestStageBinaryRefreshAndCorruption(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "input.exe")
	os.WriteFile(source, []byte("first version"), 0600)
	first, err := stageBinary(source, filepath.Join(root, "installed"), "core.exe")
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(first, []byte("partial copy"), 0600)
	repaired, err := stageBinary(source, filepath.Join(root, "installed"), "core.exe")
	if err != nil || repaired != first {
		t.Fatalf("repair: %s %v", repaired, err)
	}
	data, _ := os.ReadFile(repaired)
	if string(data) != "first version" {
		t.Fatal("corrupted cache reused")
	}
	os.WriteFile(source, []byte("second version"), 0600)
	second, err := stageBinary(source, filepath.Join(root, "installed"), "core.exe")
	if err != nil || first == second {
		t.Fatalf("update: %s %v", second, err)
	}
	data, _ = os.ReadFile(first)
	if string(data) != "first version" {
		t.Fatal("running executable overwritten")
	}
}

func TestDiagnosticTailIsBounded(t *testing.T) {
	var tail diagnosticTail
	tail.Write([]byte(strings.Repeat("x", 20000)))
	tail.Write([]byte("last error"))
	if len(tail.String()) != 8192 || !strings.HasSuffix(tail.String(), "last error") {
		t.Fatal("unbounded or missing diagnostic")
	}
}
