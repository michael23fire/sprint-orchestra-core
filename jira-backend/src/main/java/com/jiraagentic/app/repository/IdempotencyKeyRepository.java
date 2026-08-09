package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.IdempotencyKeyRecord;
import org.springframework.data.jpa.repository.JpaRepository;

public interface IdempotencyKeyRepository extends JpaRepository<IdempotencyKeyRecord, String> {
}
