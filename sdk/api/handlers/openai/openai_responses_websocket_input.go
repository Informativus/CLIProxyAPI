package openai

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/interfaces"
	coreauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	"github.com/tidwall/gjson"
)

// Keep terminal response identities on the downstream connection: HTTP turns
// and upstream socket replacements do not retain them in the Codex executor.
// Hashes bound memory independently of response ID length; the ring bounds it
// independently of connection lifetime. No response content is retained.
type responsesWebsocketTerminalResponses struct {
	mu    sync.Mutex
	ids   [256][sha256.Size]byte
	count int
	next  int
}

func (s *responsesWebsocketTerminalResponses) record(payload []byte) {
	switch gjson.GetBytes(payload, "type").String() {
	case "response.completed", "response.done", "response.incomplete", "response.failed", "response.cancelled":
	default:
		return
	}
	responseID := gjson.GetBytes(payload, "response.id")
	if responseID.Type != gjson.String || strings.TrimSpace(responseID.String()) == "" {
		return
	}
	id := sha256.Sum256([]byte(responseID.String()))
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ids[s.next] = id
	s.next = (s.next + 1) % len(s.ids)
	if s.count < len(s.ids) {
		s.count++
	}
}

func (s *responsesWebsocketTerminalResponses) contains(payload []byte) bool {
	responseID := gjson.GetBytes(payload, "response_id")
	if responseID.Type != gjson.String || strings.TrimSpace(responseID.String()) == "" {
		return false
	}
	id := sha256.Sum256([]byte(responseID.String()))
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := 0; i < s.count; i++ {
		if s.ids[i] == id {
			return true
		}
	}
	return false
}

// responsesLocalInterrupt cancels an in-flight HTTP turn. A websocket upstream
// does not use it; that interrupt is written to the existing socket instead.
type responsesLocalInterrupt struct {
	mu     sync.Mutex
	active bool
	frames chan []byte
}

func newResponsesLocalInterrupt() *responsesLocalInterrupt {
	return &responsesLocalInterrupt{frames: make(chan []byte, 1)}
}

func (s *responsesLocalInterrupt) begin() {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.active = true
	s.mu.Unlock()
}

func (s *responsesLocalInterrupt) end() {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.active = false
	select {
	case <-s.frames:
	default:
	}
	s.mu.Unlock()
}

func (s *responsesLocalInterrupt) deliver(payload []byte) bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.active {
		return false
	}
	select {
	case s.frames <- bytes.Clone(payload):
	default:
	}
	return true
}

func (s *responsesLocalInterrupt) framesChan() <-chan []byte {
	if s == nil {
		return nil
	}
	return s.frames
}

// readResponsesWebsocketInput is the only downstream reader. Interrupts are
// handled immediately so they are not queued behind an active response.
// The bounded queue backpressures clients instead of retaining unlimited input.
func readResponsesWebsocketInput(ctx context.Context, cancel context.CancelCauseFunc, conn *websocket.Conn, interrupt func([]byte) error, local *responsesLocalInterrupt, writer *responsesWebsocketWriter, timeline websocketTimelineAppender) (<-chan cliproxyexecutor.WebsocketInput, <-chan struct{}) {
	input := make(chan cliproxyexecutor.WebsocketInput, 16)
	done := make(chan struct{})
	go func() {
		defer close(done)
		defer close(input)
		for {
			kind, payload, errRead := conn.ReadMessage()
			if errRead != nil {
				cancel(errRead)
				return
			}
			if kind != websocket.TextMessage && kind != websocket.BinaryMessage {
				continue
			}
			// Control frames bypass response.create normalization. The original
			// response_id, mode, and extension fields must reach upstream unchanged.
			if json.Valid(payload) && gjson.GetBytes(payload, "type").String() == "response.interrupt" {
				appendResponsesInterruptDiagnostic(timeline, payload, "")
				// A late or duplicate interrupt is already satisfied. Check before
				// forwarding so it cannot cancel a newer HTTP turn or reach a new
				// upstream socket that no longer knows the completed response.
				if writer != nil && writer.terminalResponses.contains(payload) {
					appendResponsesInterruptDiagnostic(timeline, payload, "handled")
					continue
				}
				errInterrupt := interrupt(payload)
				switch {
				case errInterrupt == nil:
					appendResponsesInterruptDiagnostic(timeline, payload, "handled")
					continue
				case errors.Is(errInterrupt, cliproxyexecutor.ErrNoActiveUpstreamWebsocket) && local.deliver(payload):
					appendResponsesInterruptDiagnostic(timeline, payload, "local_http")
					continue
				default:
					appendResponsesInterruptDiagnostic(timeline, payload, "rejected")
					// The sanitized outcome replaces raw error logging here: errors
					// from transport dependencies may contain credentials or URLs.
					_, errWrite := writeResponsesWebsocketError(writer, nil, &interfaces.ErrorMessage{
						StatusCode: http.StatusBadRequest,
						Error:      errInterrupt,
					})
					if errWrite != nil {
						cancel(errWrite)
						return
					}
					continue
				}
			}
			select {
			case input <- cliproxyexecutor.WebsocketInput{Payload: payload}:
			case <-ctx.Done():
				return
			}
		}
	}()
	return input, done
}

func appendResponsesInterruptDiagnostic(timeline websocketTimelineAppender, payload []byte, outcome string) {
	if timeline == nil {
		return
	}
	// Hash client-controlled IDs and omit all arbitrary fields, including mode.
	// Only correlation metadata is needed to diagnose control-frame timing.
	idHash := sha256.Sum256([]byte(gjson.GetBytes(payload, "response_id").String()))
	diagnostic := struct {
		Type           string `json:"type"`
		ResponseIDHash string `json:"response_id_hash"`
		Outcome        string `json:"outcome,omitempty"`
	}{Type: "response.interrupt", ResponseIDHash: fmt.Sprintf("%x", idHash[:8]), Outcome: outcome}
	data, errMarshal := json.Marshal(diagnostic)
	if errMarshal != nil {
		return
	}
	event := "interrupt"
	if outcome != "" {
		event = "interrupt.outcome"
	}
	timeline.Append(event, data, time.Now())
}

func (h *OpenAIResponsesAPIHandler) forwardResponsesWebsocketInterrupt(ctx context.Context, sessionID string, payload []byte) error {
	responseID := gjson.GetBytes(payload, "response_id")
	if responseID.Type != gjson.String || strings.TrimSpace(responseID.String()) == "" {
		return fmt.Errorf("response.interrupt requires response_id")
	}
	if h == nil || h.AuthManager == nil {
		return cliproxyexecutor.ErrNoActiveUpstreamWebsocket
	}
	exec, ok := h.AuthManager.Executor("codex")
	if !ok || exec == nil {
		return cliproxyexecutor.ErrNoActiveUpstreamWebsocket
	}
	sender, ok := exec.(interface {
		InterruptExecutionSession(context.Context, string, []byte) error
	})
	if !ok || sender == nil {
		return fmt.Errorf("upstream executor does not support response.interrupt")
	}
	ctx = cliproxyexecutor.WithWebsocketAuthCheck(ctx, func(authID string) bool {
		// Home runtime credentials live on the execution session, not in the global map.
		auth, found := h.AuthManager.GetByID(authID)
		if !found || auth == nil {
			auth, found = h.AuthManager.GetExecutionSessionAuthByID(sessionID, authID)
		}
		return found && auth != nil && !auth.Disabled && auth.Status != coreauth.StatusDisabled
	})
	return sender.InterruptExecutionSession(ctx, sessionID, payload)
}
