package main

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sync"
)

type invocation struct {
	command, name, directory string
	args                     []string
}

func parseInvocation(args []string) (invocation, error) {
	var in invocation
	if len(args) < 2 {
		return in, errors.New("usage: sbc-service <install|exists|status|start|stop|restart|remove|serve> <service-name> [--work-dir DIR -- PROGRAM ARGS...]")
	}
	in.command, in.name = args[0], args[1]
	if !regexp.MustCompile(`^[A-Za-z0-9_-]+$`).MatchString(in.name) {
		return in, errors.New("invalid service name")
	}
	switch in.command {
	case "install", "serve":
		flags := flag.NewFlagSet(in.command, flag.ContinueOnError)
		flags.SetOutput(io.Discard)
		flags.StringVar(&in.directory, "work-dir", "", "working directory")
		if err := flags.Parse(args[2:]); err != nil {
			return in, err
		}
		in.args = flags.Args()
		if !filepath.IsAbs(in.directory) || len(in.args) == 0 || !filepath.IsAbs(in.args[0]) {
			return in, errors.New("absolute working directory and executable paths are required")
		}
	case "exists", "status", "start", "stop", "restart", "remove":
		if len(args) != 2 {
			return in, errors.New("unexpected command arguments")
		}
	default:
		return in, fmt.Errorf("unknown command: %s", in.command)
	}
	return in, nil
}

// Never overwrite an executable that another service or uv environment is using.
func stageBinary(source, root, filename string) (string, error) {
	f, err := os.Open(source)
	if err != nil {
		return "", err
	}
	defer f.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, f); err != nil {
		return "", err
	}
	directory := filepath.Join(root, hex.EncodeToString(hash.Sum(nil)))
	if err := os.MkdirAll(directory, 0755); err != nil {
		return "", err
	}
	destination := filepath.Join(directory, filename)
	// Verify cached contents too; a partial copy must never become the service target.
	if data, err := os.ReadFile(destination); err == nil {
		sum := sha256.Sum256(data)
		if hex.EncodeToString(sum[:]) == filepath.Base(directory) {
			return destination, nil
		}
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return "", err
	}
	tmp, err := os.CreateTemp(directory, "copy-*.exe")
	if err != nil {
		return "", err
	}
	defer os.Remove(tmp.Name())
	_, copyErr := io.Copy(tmp, f)
	syncErr := tmp.Sync()
	closeErr := tmp.Close()
	if err := errors.Join(copyErr, syncErr, closeErr); err != nil {
		return "", err
	}
	if err := os.Rename(tmp.Name(), destination); err != nil {
		return "", err
	}
	return destination, nil
}

// Only keep a bounded diagnostic tail, never an unbounded service debug log.
type diagnosticTail struct {
	mu   sync.Mutex
	data []byte
}

func (t *diagnosticTail) Write(p []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	const limit = 8192
	n := len(p)
	if len(p) >= limit {
		t.data = append(t.data[:0], p[len(p)-limit:]...)
	} else {
		t.data = append(t.data, p...)
		if len(t.data) > limit {
			t.data = t.data[len(t.data)-limit:]
		}
	}
	return n, nil
}

func (t *diagnosticTail) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.data)
}
