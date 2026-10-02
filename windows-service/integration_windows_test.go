//go:build windows

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/eventlog"
	"golang.org/x/sys/windows/svc/mgr"
)

// This test exercises the real SCM, real service process and real child signals.
// It requires an elevated Windows runner and is opt-in outside CI.
func TestSCMLifecycleAndMigration(t *testing.T) {
	if os.Getenv("SBC_SERVICE_INTEGRATION") != "1" {
		t.Skip("set SBC_SERVICE_INTEGRATION=1 on an elevated Windows host")
	}
	if !windows.GetCurrentProcessToken().IsElevated() {
		t.Fatal("SCM integration tests require administrator rights")
	}
	dir := filepath.Join(t.TempDir(), "Program Files", "Test User 代理配置")
	if err := os.MkdirAll(dir, 0755); err != nil {
		t.Fatal(err)
	}
	helper, core := filepath.Join(dir, "sbc-service.exe"), filepath.Join(dir, "sing-box.exe")
	for _, build := range [][]string{{"build", "-o", helper, "."}, {"build", "-o", core, "./testdata/core.go"}} {
		if output, err := exec.Command("go", build...).CombinedOutput(); err != nil {
			t.Fatalf("build: %v %s", err, output)
		}
	}
	name := "sbc-test-" + strconv.FormatInt(time.Now().UnixNano(), 10)
	m, err := mgr.Connect()
	if err != nil {
		t.Fatal(err)
	}
	defer m.Disconnect()
	t.Cleanup(func() {
		if s, err := m.OpenService(name); err == nil {
			stopService(s)
			s.Delete()
			s.Close()
		}
		eventlog.Remove(name + "-native")
		if root, err := windows.KnownFolderPath(windows.FOLDERID_ProgramFiles, 0); err == nil {
			os.RemoveAll(filepath.Join(root, "sing-box-cli", name))
		}
	})
	// Simulate the stopped SCM entry left by NSSM; installation must update it
	// in place rather than deleting it or replacing its account/display name.
	legacy, err := m.CreateService(name, core, mgr.Config{StartType: mgr.StartManual, DisplayName: "Existing sing-box service"})
	if err != nil {
		t.Fatal(err)
	}
	oldConfig, err := legacy.Config()
	legacy.Close()
	if err != nil {
		t.Fatal(err)
	}
	invoke := func(args ...string) string {
		t.Helper()
		output, err := exec.Command(helper, append([]string{args[0], name}, args[1:]...)...).CombinedOutput()
		if err != nil {
			t.Fatalf("%s: %v %s", args, err, output)
		}
		return strings.TrimSpace(string(output))
	}
	installArgs := []string{"install", "--work-dir", dir, "--", core, "run", "-c", filepath.Join(dir, "config.json"), "-D", dir}
	invoke(installArgs...)
	s, err := m.OpenService(name)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	updated, err := s.Config()
	if err != nil {
		t.Fatal(err)
	}
	if updated.ServiceStartName != oldConfig.ServiceStartName || updated.DisplayName != oldConfig.DisplayName || updated.StartType != mgr.StartAutomatic {
		t.Fatalf("legacy settings lost: %#v", updated)
	}
	if !strings.Contains(updated.BinaryPathName, "serve") || strings.Contains(updated.BinaryPathName, helper) {
		t.Fatalf("service depends on source environment: %s", updated.BinaryPathName)
	}
	invoke("start")
	invoke("start") // Idempotent.
	if state := invoke("status"); state != "SERVICE_RUNNING" {
		t.Fatal(state)
	}
	pidBytes, err := os.ReadFile(filepath.Join(dir, "core.pid"))
	if err != nil {
		t.Fatal(err)
	}
	pid, err := strconv.Atoi(string(pidBytes))
	if err != nil {
		t.Fatal(err)
	}
	invoke("stop")
	invoke("stop")
	if _, err := os.Stat(filepath.Join(dir, "graceful-stop")); err != nil {
		t.Fatalf("core was not stopped gracefully: %v", err)
	}
	if p, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(pid)); err == nil {
		defer windows.CloseHandle(p)
		state, err := windows.WaitForSingleObject(p, 5000)
		if err != nil || state != windows.WAIT_OBJECT_0 {
			t.Fatalf("orphan core: state %d, %v", state, err)
		}
	}
	invoke("start")
	invoke("restart")
	// Updating a running service to a target that fails must restore the old
	// ImagePath, recover its running state and report failure to Python.
	badArgs := append([]string(nil), installArgs...)
	badArgs[7] = "fail-start"
	output, err := exec.Command(helper, append([]string{badArgs[0], name}, badArgs[1:]...)...).CombinedOutput()
	if err == nil {
		t.Fatalf("failed startup was accepted: %s", output)
	}
	restored, err := s.Config()
	if err != nil || restored.BinaryPathName != updated.BinaryPathName {
		t.Fatalf("rollback failed: %#v %v %s", restored, err, output)
	}
	if status, err := s.Query(); err != nil || status.State != svc.Running {
		t.Fatalf("old service not recovered: %#v %v", status, err)
	}
	invoke("remove")
	s.Close()
	missing := exec.Command(helper, "exists", name).Run()
	if exit, ok := missing.(*exec.ExitError); !ok || exit.ExitCode() != 1060 {
		t.Fatalf("missing service exit code: %v", missing)
	}
	invoke("remove")
}
