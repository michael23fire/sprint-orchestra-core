package com.jiraagentic.app.security;

import com.jiraagentic.app.entity.IdempotencyKeyRecord;
import com.jiraagentic.app.repository.IdempotencyKeyRepository;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;
import org.springframework.web.filter.OncePerRequestFilter;
import org.springframework.web.util.ContentCachingResponseWrapper;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Optional;

/**
 * Deduplicates a mutating request that a caller retries after losing the original response — the ai-service
 * commit loop sends a stable {@code Idempotency-Key} (thread_id : escalation_round : plan_revision_round :
 * action_index) with each Jira write it makes; jira-backend actually executing the write and the caller
 * finding out both succeeded were previously two separate, unguarded events — a network timeout between them
 * (server commits, response is lost) meant a retry re-executed the same side effect (e.g. a duplicate
 * comment). This closes that gap for the *sequential retry* case: request comes in, the exact same key was
 * already recorded with a 2xx outcome, the stored response is replayed and the handler never runs a second
 * time.
 *
 * <p><b>Deliberately not a lock against true concurrency</b>: two requests carrying the same key that arrive
 * genuinely in parallel can both miss the "already recorded" check and both execute the side effect; the
 * unique constraint on {@code idempotency_keys.idempotency_key} only guarantees one of the two response
 * records survives, not that only one side effect happened. That's an acceptable, named bound for this
 * codebase's actual failure mode (a single caller retrying after a timeout, not concurrent callers racing
 * the same key) — same "pragmatic bound, not a guarantee" shape as `_AUTO_REEVALUATE_PROPAGATION_DELAY_SECONDS`
 * on the ai-service side.
 *
 * <p>Gated purely on header presence, not on path — the vast majority of requests (anything not coming from
 * `JiraActionsClient`) never send this header and pass through untouched.
 */
@Component
public class IdempotencyFilter extends OncePerRequestFilter {

    public static final String IDEMPOTENCY_KEY_HEADER = "Idempotency-Key";

    private final IdempotencyKeyRepository repository;

    public IdempotencyFilter(IdempotencyKeyRepository repository) {
        this.repository = repository;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        return !request.getRequestURI().startsWith("/api/");
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain
    ) throws ServletException, IOException {
        String key = request.getHeader(IDEMPOTENCY_KEY_HEADER);
        if (!StringUtils.hasText(key)) {
            filterChain.doFilter(request, response);
            return;
        }

        Optional<IdempotencyKeyRecord> existing = repository.findById(key);
        if (existing.isPresent()) {
            replay(response, existing.get());
            return;
        }

        ContentCachingResponseWrapper wrapped = new ContentCachingResponseWrapper(response);
        filterChain.doFilter(request, wrapped);

        if (wrapped.getStatus() >= 200 && wrapped.getStatus() < 300) {
            String body = new String(wrapped.getContentAsByteArray(), StandardCharsets.UTF_8);
            IdempotencyKeyRecord record = new IdempotencyKeyRecord(
                    key, wrapped.getStatus(), body, wrapped.getContentType(), Instant.now()
            );
            try {
                repository.save(record);
            } catch (DataIntegrityViolationException raced) {
                // A concurrent request with the same key won the insert first — the side effect it
                // guarded against duplicating already happened once either way; nothing left to do.
            }
        }
        wrapped.copyBodyToResponse();
    }

    private void replay(HttpServletResponse response, IdempotencyKeyRecord record) throws IOException {
        response.setStatus(record.getResponseStatus());
        if (StringUtils.hasText(record.getContentType())) {
            response.setContentType(record.getContentType());
        }
        if (record.getResponseBody() != null) {
            response.getWriter().write(record.getResponseBody());
        }
    }
}
