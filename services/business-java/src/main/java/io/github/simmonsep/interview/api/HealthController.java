package io.github.simmonsep.interview.api;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class HealthController {
    // Liveness alone: this initial module has no SQL, Redis or Agent connection.
    @GetMapping("/health")
    public HealthResponse health() {
        return new HealthResponse("ok", "java-interview-business", "process_liveness");
    }

    public record HealthResponse(String status, String service, String scope) {}
}
