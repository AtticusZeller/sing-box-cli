// A stand-in core for SCM lifecycle tests, never packaged or used in production.
package main

import (
	"fmt"
	"os"
	"os/signal"
)

func main() {
	if len(os.Args) < 2 {
		os.Exit(2)
	}
	if os.Args[1] == "check" {
		return
	}
	for _, arg := range os.Args[2:] {
		if arg == "fail-start" {
			fmt.Fprintln(os.Stderr, "test core startup failure")
			os.Exit(2)
		}
	}
	if err := os.WriteFile("core.pid", []byte(fmt.Sprint(os.Getpid())), 0644); err != nil {
		panic(err)
	}
	interrupt := make(chan os.Signal, 1)
	signal.Notify(interrupt, os.Interrupt)
	<-interrupt
	os.WriteFile("graceful-stop", []byte("ok"), 0644)
}
