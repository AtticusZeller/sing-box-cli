//go:build windows

package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/eventlog"
	"golang.org/x/sys/windows/svc/mgr"
)

func main() {
	in, err := parseInvocation(os.Args[1:])
	if err == nil {
		if in.command == "serve" {
			err = svc.Run(in.name, &host{in: in})
		} else {
			err = manage(in)
		}
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		var errno syscall.Errno
		if errors.As(err, &errno) {
			os.Exit(int(errno))
		}
		os.Exit(1)
	}
}

func manage(in invocation) error {
	m, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	if in.command == "install" {
		return install(m, in)
	}
	s, err := m.OpenService(in.name)
	if errors.Is(err, windows.ERROR_SERVICE_DOES_NOT_EXIST) && (in.command == "remove" || in.command == "stop") {
		return nil
	}
	if err != nil {
		return err
	}
	defer s.Close()
	switch in.command {
	case "exists":
		return nil
	case "status":
		status, err := s.Query()
		if err != nil {
			return err
		}
		fmt.Println(stateName(status.State))
		return nil
	case "start":
		return startService(s)
	case "stop":
		return stopService(s)
	case "restart":
		if err := stopService(s); err != nil {
			return err
		}
		return startService(s)
	case "remove":
		if err := stopService(s); err != nil {
			return err
		}
		return s.Delete()
	}
	return fmt.Errorf("unsupported operation: %s", in.command)
}

func stateName(state svc.State) string {
	names := map[svc.State]string{svc.Stopped: "SERVICE_STOPPED", svc.StartPending: "SERVICE_START_PENDING", svc.StopPending: "SERVICE_STOP_PENDING", svc.Running: "SERVICE_RUNNING", svc.ContinuePending: "SERVICE_CONTINUE_PENDING", svc.PausePending: "SERVICE_PAUSE_PENDING", svc.Paused: "SERVICE_PAUSED"}
	if name, ok := names[state]; ok {
		return name
	}
	return fmt.Sprintf("SERVICE_UNKNOWN_%d", state)
}

func waitState(s *mgr.Service, wanted svc.State) error {
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		status, err := s.Query()
		if err != nil {
			return err
		}
		if status.State == wanted {
			return nil
		}
		if wanted == svc.Running && status.State == svc.Stopped {
			return fmt.Errorf("service stopped during startup (Windows exit %d, service exit %d)", status.Win32ExitCode, status.ServiceSpecificExitCode)
		}
		time.Sleep(100 * time.Millisecond)
	}
	return fmt.Errorf("timed out waiting for %s", stateName(wanted))
}

func startService(s *mgr.Service) error {
	status, err := s.Query()
	if err != nil {
		return err
	}
	if status.State == svc.Running {
		return nil
	}
	if status.State == svc.StopPending {
		if err := waitState(s, svc.Stopped); err != nil {
			return err
		}
	}
	if status.State != svc.StartPending {
		if err := s.Start(); err != nil && !errors.Is(err, windows.ERROR_SERVICE_ALREADY_RUNNING) {
			return err
		}
	}
	return waitState(s, svc.Running)
}

func stopService(s *mgr.Service) error {
	status, err := s.Query()
	if err != nil {
		return err
	}
	if status.State == svc.Stopped {
		return nil
	}
	if status.State == svc.StartPending {
		if err := waitState(s, svc.Running); err != nil {
			return err
		}
		status.State = svc.Running
	}
	if status.State != svc.StopPending {
		if _, err := s.Control(svc.Stop); err != nil && !errors.Is(err, windows.ERROR_SERVICE_NOT_ACTIVE) {
			return err
		}
	}
	return waitState(s, svc.Stopped)
}

func install(m *mgr.Mgr, in invocation) error {
	// Validate using the chosen core, without changing the user's configuration.
	if len(in.args) < 2 || in.args[1] != "run" {
		return errors.New("expected sing-box run arguments")
	}
	check := exec.Command(in.args[0], append([]string{"check"}, in.args[2:]...)...)
	check.Dir = in.directory
	if output, err := check.CombinedOutput(); err != nil {
		return fmt.Errorf("configuration check failed: %w: %s", err, output)
	}
	programFiles, err := windows.KnownFolderPath(windows.FOLDERID_ProgramFiles, 0)
	if err != nil {
		return err
	}
	root := filepath.Join(programFiles, "sing-box-cli", in.name)
	self, err := os.Executable()
	if err != nil {
		return err
	}
	helper, err := stageBinary(self, root, "sbc-service.exe")
	if err != nil {
		return err
	}
	core, err := stageBinary(in.args[0], root, "sing-box.exe")
	if err != nil {
		return err
	}
	args := append([]string{core}, in.args[1:]...)
	image := windows.ComposeCommandLine(append([]string{helper, "serve", in.name, "--work-dir", in.directory, "--"}, args...))
	if err := ensureEventSource(in.name); err != nil {
		return err
	}
	s, err := m.OpenService(in.name)
	if errors.Is(err, windows.ERROR_SERVICE_DOES_NOT_EXIST) {
		s, err = m.CreateService(in.name, helper, mgr.Config{StartType: mgr.StartAutomatic, DisplayName: "sing-box service"}, append([]string{"serve", in.name, "--work-dir", in.directory, "--"}, args...)...)
		if err != nil {
			return err
		}
		defer s.Close()
		if err := configureRecovery(s); err != nil {
			return errors.Join(err, s.Delete())
		}
		return nil
	}
	if err != nil {
		return err
	}
	defer s.Close()
	old, err := s.Config()
	if err != nil {
		return err
	}
	actions, err := s.RecoveryActions()
	if err != nil {
		return err
	}
	reset, err := s.ResetPeriod()
	if err != nil {
		return err
	}
	nonCrash, err := s.RecoveryActionsOnNonCrashFailures()
	if err != nil {
		return err
	}
	status, err := s.Query()
	if err != nil {
		return err
	}
	if status.State == svc.StartPending {
		if err := waitState(s, svc.Running); err != nil {
			return err
		}
		status.State = svc.Running
	}
	wasRunning := status.State == svc.Running || status.State == svc.Paused
	changed := old.BinaryPathName != image
	if changed {
		if err := stopService(s); err != nil {
			return err
		}
	}
	updated := old // Preserve the service name, account, dependencies and other settings.
	updated.BinaryPathName, updated.StartType = image, mgr.StartAutomatic
	applyErr := s.UpdateConfig(updated)
	if applyErr == nil {
		applyErr = configureRecovery(s)
	}
	if applyErr == nil && changed && wasRunning {
		applyErr = startService(s)
	}
	if applyErr == nil {
		return nil
	}
	// Restore the old SCM target on failed NSSM migration or native upgrade.
	stopErr := stopService(s)
	rollbackErr := s.UpdateConfig(old)
	recoveryErr := s.SetRecoveryActions(actions, reset)
	flagErr := s.SetRecoveryActionsOnNonCrashFailures(nonCrash)
	var restartErr error
	if wasRunning && rollbackErr == nil {
		restartErr = startService(s)
	}
	return errors.Join(fmt.Errorf("service update failed: %w", applyErr), stopErr, rollbackErr, recoveryErr, flagErr, restartErr)
}

func configureRecovery(s *mgr.Service) error {
	// A finite recovery sequence avoids an endless crash loop on invalid config.
	err := s.SetRecoveryActions([]mgr.RecoveryAction{{Type: mgr.ServiceRestart, Delay: 2 * time.Second}, {Type: mgr.ServiceRestart, Delay: 5 * time.Second}, {Type: mgr.NoAction}}, 86400)
	if err != nil {
		return err
	}
	return s.SetRecoveryActionsOnNonCrashFailures(true)
}

func ensureEventSource(name string) error {
	source := name + "-native"
	key, err := registry.OpenKey(registry.LOCAL_MACHINE, `SYSTEM\CurrentControlSet\Services\EventLog\Application\`+source, registry.QUERY_VALUE)
	if err == nil {
		return key.Close()
	}
	if !errors.Is(err, windows.ERROR_FILE_NOT_FOUND) {
		return err
	}
	return eventlog.InstallAsEventCreate(source, eventlog.Warning|eventlog.Error)
}

type host struct{ in invocation }

func (h *host) Execute(_ []string, requests <-chan svc.ChangeRequest, changes chan<- svc.Status) (bool, uint32) {
	changes <- svc.Status{State: svc.StartPending, WaitHint: 15000}
	log, _ := eventlog.Open(h.in.name + "-native")
	if log != nil {
		defer log.Close()
	}
	report := func(message string) {
		if log != nil {
			log.Error(1, message)
		}
	}
	// SCM processes do not have a console. A hidden console allows CTRL_BREAK
	// to reach a dedicated child process group for graceful sing-box shutdown.
	kernel := windows.NewLazySystemDLL("kernel32.dll")
	if ok, _, err := kernel.NewProc("AllocConsole").Call(); ok == 0 {
		report(fmt.Sprint("AllocConsole: ", err))
		return false, 1
	}
	defer kernel.NewProc("FreeConsole").Call()
	window, _, _ := kernel.NewProc("GetConsoleWindow").Call()
	windows.NewLazySystemDLL("user32.dll").NewProc("ShowWindow").Call(window, 0)
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		report(err.Error())
		return false, 1
	}
	defer windows.CloseHandle(job)
	limits := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	limits.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(job, windows.JobObjectExtendedLimitInformation, uintptr(unsafe.Pointer(&limits)), uint32(unsafe.Sizeof(limits))); err != nil {
		report(err.Error())
		return false, 1
	}
	var tail diagnosticTail
	cmd := exec.Command(h.in.args[0], h.in.args[1:]...)
	cmd.Dir = h.in.directory
	cmd.Stdout, cmd.Stderr = &tail, &tail
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NEW_PROCESS_GROUP, HideWindow: true}
	if err := cmd.Start(); err != nil {
		report(err.Error())
		return false, 1
	}
	process, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(cmd.Process.Pid))
	if err == nil {
		err = windows.AssignProcessToJobObject(job, process)
		windows.CloseHandle(process)
	}
	if err != nil {
		cmd.Process.Kill()
		cmd.Wait()
		report(err.Error())
		return false, 1
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		report(fmt.Sprintf("core exited during startup: %v\n%s", err, tail.String()))
		return false, 1
	case <-time.After(500 * time.Millisecond):
	}
	current := svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	changes <- current
	for {
		select {
		case err := <-done:
			report(fmt.Sprintf("core exited unexpectedly: %v\n%s", err, tail.String()))
			return false, 1
		case request, ok := <-requests:
			if !ok {
				return false, 1
			}
			switch request.Cmd {
			case svc.Interrogate:
				changes <- current
			case svc.Stop, svc.Shutdown:
				changes <- svc.Status{State: svc.StopPending, WaitHint: 15000}
				signalErr := windows.GenerateConsoleCtrlEvent(windows.CTRL_BREAK_EVENT, uint32(cmd.Process.Pid))
				if signalErr == nil {
					select {
					case <-done:
						return false, 0
					case <-time.After(10 * time.Second):
					}
				}
				if log != nil {
					log.Warning(2, "Core did not stop gracefully; terminating its process tree.")
				}
				if err := windows.TerminateJobObject(job, 1); err != nil {
					report(err.Error())
					return false, 1
				}
				select {
				case <-done:
					return false, 0
				case <-time.After(5 * time.Second):
					report("Core shutdown timed out")
					return false, 1
				}
			}
		}
	}
}
