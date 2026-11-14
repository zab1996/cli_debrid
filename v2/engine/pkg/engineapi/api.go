// Package engineapi is the small localhost-only JSON API the cli_debrid 2.0
// Python app uses to drive the engine. It replaces decypharr's pkg/server
// (web UI, qBittorrent/SABnzbd emulation, WebDAV, repair pages).
package engineapi

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/rs/zerolog"
	"github.com/sirrobot01/decypharr/internal/config"
	"github.com/sirrobot01/decypharr/internal/logger"
	"github.com/sirrobot01/decypharr/internal/utils"
	"github.com/sirrobot01/decypharr/pkg/arr"
	debrid "github.com/sirrobot01/decypharr/pkg/debrid/common"
	"github.com/sirrobot01/decypharr/pkg/manager"
	"github.com/sirrobot01/decypharr/pkg/storage"
)

// Category every add is filed under (decypharr requires an "arr").
const Category = "clid2"

type Server struct {
	mgr *manager.Manager
	log zerolog.Logger
	srv *http.Server
}

func New(mgr *manager.Manager) *Server {
	return &Server{mgr: mgr, log: logger.New("engineapi")}
}

// Handler exposes the routes (also used by tests).
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /status", s.status)
	mux.HandleFunc("GET /cache", s.cache)
	mux.HandleFunc("GET /streams", s.streams)
	mux.HandleFunc("POST /add", s.add)
	mux.HandleFunc("GET /entries/{id}", s.getEntry)
	mux.HandleFunc("DELETE /entries/{id}", s.deleteEntry)
	mux.HandleFunc("POST /entries/{id}/health", s.checkHealth)
	mux.HandleFunc("GET /entries/{id}/health", s.getHealth)
	return mux
}

// Start serves on cfg.BindAddress:cfg.Port (127.0.0.1 by default) until ctx ends.
func (s *Server) Start(ctx context.Context) error {
	cfg := config.Get()
	host := cfg.BindAddress
	if host == "" || host == "0.0.0.0" {
		host = "127.0.0.1"
	}
	s.srv = &http.Server{
		Addr:              net.JoinHostPort(host, cfg.Port),
		Handler:           s.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	errCh := make(chan error, 1)
	go func() {
		s.log.Info().Str("addr", s.srv.Addr).Msg("engine API listening")
		errCh <- s.srv.ListenAndServe()
	}()
	select {
	case <-ctx.Done():
		shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return s.srv.Shutdown(shutCtx)
	case err := <-errCh:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}

// ---------------------------------------------------------------- helpers

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, err error) {
	writeJSON(w, code, map[string]string{"error": err.Error()})
}

func (s *Server) findEntry(id string) *storage.Entry {
	if e, err := s.mgr.Queue().GetTorrent(id); err == nil && e != nil {
		return e
	}
	if e, err := s.mgr.GetEntry(id); err == nil && e != nil {
		return e
	}
	if e, err := s.mgr.GetEntry(strings.ToLower(id)); err == nil && e != nil {
		return e
	}
	return nil
}

func ownArr() *arr.Arr {
	no := false
	return arr.New(Category, "", "", true, &no, "", "")
}

// ---------------------------------------------------------------- handlers

func (s *Server) status(w http.ResponseWriter, r *http.Request) {
	cfg := config.Get()
	ready := false
	select {
	case <-s.mgr.IsReady():
		ready = true
	default:
	}
	var providers []string
	s.mgr.Clients().Range(func(name string, _ debrid.Client) bool {
		providers = append(providers, name)
		return true
	})
	sort.Strings(providers)
	mountPath := cfg.Mount.MountPath
	mounted := false
	if mountPath != "" {
		if entries, err := os.ReadDir(mountPath); err == nil && len(entries) > 0 {
			mounted = true
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ready":      ready,
		"mount_path": mountPath,
		"mounted":    mounted,
		"providers":  providers,
		"usenet":     s.mgr.HasUsenet(),
		"uptime_s":   int(s.mgr.Uptime().Seconds()),
	})
}

// GET /streams -> files being read through the mount right now, plus usenet
// connection usage, for the home page.
func (s *Server) streams(w http.ResponseWriter, r *http.Request) {
	streams := s.mgr.GetActiveStreams()
	sort.Slice(streams, func(i, j int) bool { return streams[i].StartedAt > streams[j].StartedAt })
	out := map[string]any{"streams": streams}
	if us := s.mgr.UsenetStats(); us != nil {
		if pool, ok := us["pool"].(map[string]interface{}); ok {
			out["usenet"] = map[string]any{"active": pool["active"], "max": pool["max_connections"]}
		}
	}
	writeJSON(w, http.StatusOK, out)
}

// GET /cache?hashes=a,b -> {"a": true, "b": false}. A hash is cached if any
// configured debrid reports it available. Providers that no longer support
// availability checks (Real-Debrid) simply report false.
func (s *Server) cache(w http.ResponseWriter, r *http.Request) {
	var hashes []string
	for _, h := range strings.Split(r.URL.Query().Get("hashes"), ",") {
		if h = strings.ToLower(strings.TrimSpace(h)); h != "" {
			hashes = append(hashes, h)
		}
	}
	out := make(map[string]bool, len(hashes))
	for _, h := range hashes {
		out[h] = false
	}
	if len(hashes) > 0 {
		s.mgr.Clients().Range(func(_ string, c debrid.Client) bool {
			for h, ok := range c.IsAvailable(hashes) {
				if ok {
					out[strings.ToLower(h)] = true
				}
			}
			return true
		})
	}
	writeJSON(w, http.StatusOK, out)
}

type addRequest struct {
	Magnet string `json:"magnet"`
	NZBB64 string `json:"nzb_b64"`
	Name   string `json:"name"`
	// Allow uncached torrents to download on the debrid side. Default false:
	// an uncached torrent is rejected, so the caller moves to the next release.
	DownloadUncached bool `json:"download_uncached"`
}

func (s *Server) add(w http.ResponseWriter, r *http.Request) {
	var req addRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<20)).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	cfg := config.Get()
	ctx := r.Context()
	switch {
	case req.Magnet != "":
		magnet, err := utils.GetMagnetFromUrl(req.Magnet, false)
		if err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		a := ownArr()
		uncached := req.DownloadUncached
		ir := manager.NewTorrentRequest("", cfg.DownloadFolder, magnet, a, config.DownloadActionNone,
			&uncached, "", manager.ImportTypeAPI, true)
		if err := s.mgr.AddNewTorrent(ctx, ir); err != nil {
			writeErr(w, http.StatusUnprocessableEntity, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"id": strings.ToLower(magnet.InfoHash), "protocol": "torrent"})
	case req.NZBB64 != "":
		content, err := base64.StdEncoding.DecodeString(req.NZBB64)
		if err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		name := req.Name
		if name == "" {
			name = "download"
		}
		if !strings.HasSuffix(strings.ToLower(name), ".nzb") {
			name += ".nzb"
		}
		ir := manager.NewNZBRequest(name, cfg.DownloadFolder, content, ownArr(), config.DownloadActionNone,
			"", manager.ImportTypeAPI, true)
		id, err := s.mgr.AddNewNZB(ctx, ir)
		if err != nil {
			writeErr(w, http.StatusUnprocessableEntity, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"id": id, "protocol": "nzb"})
	default:
		writeErr(w, http.StatusBadRequest, errors.New("magnet or nzb_b64 is required"))
	}
}

type fileJSON struct {
	Name string `json:"name"`
	Size int64  `json:"size"`
	Path string `json:"path"`
}

type entryJSON struct {
	ID       string     `json:"id"`
	Name     string     `json:"name"`
	Protocol string     `json:"protocol"`
	Status   string     `json:"status"` // queued | downloading | ready | error
	Progress float64    `json:"progress"`
	Error    string     `json:"error,omitempty"`
	Files    []fileJSON `json:"files"`
}

func entryStatus(e *storage.Entry) string {
	switch {
	case e.Bad || e.State == storage.EntryStateError || string(e.Status) == "error":
		return "error"
	case e.IsComplete || e.State == storage.EntryStatePausedUP:
		return "ready"
	case string(e.Status) == "queued":
		return "queued"
	default:
		return "downloading"
	}
}

func (s *Server) toJSON(e *storage.Entry) entryJSON {
	status := entryStatus(e)
	progress := e.Progress
	if progress <= 1 {
		progress *= 100
	}
	if status == "ready" {
		progress = 100
	}
	out := entryJSON{
		ID: e.InfoHash, Name: e.Name, Protocol: string(e.Protocol),
		Status: status, Progress: progress, Error: e.LastError,
	}
	if status == "ready" {
		base := s.mgr.GetTorrentMountPath(e)
		for _, f := range e.Files {
			if f == nil || f.Deleted {
				continue
			}
			out.Files = append(out.Files, fileJSON{Name: f.Name, Size: f.Size, Path: filepath.Join(base, f.Name)})
		}
		sort.Slice(out.Files, func(i, j int) bool { return out.Files[i].Name < out.Files[j].Name })
	}
	return out
}

func (s *Server) getEntry(w http.ResponseWriter, r *http.Request) {
	e := s.findEntry(r.PathValue("id"))
	if e == nil {
		writeErr(w, http.StatusNotFound, errors.New("entry not found"))
		return
	}
	writeJSON(w, http.StatusOK, s.toJSON(e))
}

func (s *Server) deleteEntry(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	e := s.findEntry(id)
	if e == nil {
		writeErr(w, http.StatusNotFound, errors.New("entry not found"))
		return
	}
	if err := s.mgr.DeleteEntry(e.InfoHash, true); err != nil {
		// Still in the add queue (never made it to storage).
		if qerr := s.mgr.Queue().Delete(e.InfoHash, nil); qerr != nil {
			writeErr(w, http.StatusInternalServerError, err)
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]bool{"deleted": true})
}

func healthStatus(h *storage.EntryHealth) string {
	if h == nil {
		return "unknown"
	}
	switch v := string(h.Status); v {
	case "healthy", "broken":
		return v
	case "repairing":
		return "checking"
	default:
		if h.ActiveRunID != "" {
			return "checking"
		}
		return "unknown"
	}
}

func (s *Server) checkHealth(w http.ResponseWriter, r *http.Request) {
	e := s.findEntry(r.PathValue("id"))
	if e == nil {
		writeErr(w, http.StatusNotFound, errors.New("entry not found"))
		return
	}
	h, err := s.mgr.Repair().RecheckEntry(s.mgr.Context(), e.Name, false)
	if err != nil {
		writeErr(w, http.StatusConflict, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "checking", "previous": healthStatus(h)})
}

func (s *Server) getHealth(w http.ResponseWriter, r *http.Request) {
	e := s.findEntry(r.PathValue("id"))
	if e == nil {
		writeErr(w, http.StatusNotFound, errors.New("entry not found"))
		return
	}
	h, err := s.mgr.Storage().GetEntryHealth(e.Name)
	if err != nil || h == nil {
		writeJSON(w, http.StatusOK, map[string]string{"status": "unknown"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": healthStatus(h), "broken_files": h.BrokenFiles, "reason": h.FailureReason,
	})
}
