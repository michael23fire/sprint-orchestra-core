package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.IssueAttachmentDto;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.IssueAttachment;
import com.jiraagentic.app.repository.IssueAttachmentRepository;
import com.jiraagentic.app.repository.IssueRepository;
import com.jiraagentic.app.event.AttachmentDeletedEvent;
import com.jiraagentic.app.event.AttachmentUploadedEvent;
import com.jiraagentic.app.entity.Comment;
import com.jiraagentic.app.repository.CommentRepository;
import com.jiraagentic.app.repository.UserRepository;
import com.jiraagentic.app.util.AttachmentReferenceExtractor;
import lombok.RequiredArgsConstructor;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.core.io.ByteArrayResource;
import org.springframework.core.io.Resource;
import org.springframework.http.ContentDisposition;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.multipart.MultipartFile;

import java.io.IOException;
import java.time.Instant;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;

import software.amazon.awssdk.core.ResponseBytes;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.CreateBucketRequest;
import software.amazon.awssdk.services.s3.model.DeleteObjectRequest;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.model.HeadBucketRequest;
import software.amazon.awssdk.services.s3.model.NoSuchBucketException;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import software.amazon.awssdk.services.s3.model.S3Exception;

@Service
@RequiredArgsConstructor
public class IssueAttachmentService {

    private final IssueAttachmentRepository issueAttachmentRepository;
    private final IssueRepository issueRepository;
    private final CommentRepository commentRepository;
    private final UserRepository userRepository;
    private final IssueHistoryService issueHistoryService;
    private final ActiveSpaceGuard activeSpaceGuard;
    private final S3Client s3Client;
    private final ApplicationEventPublisher applicationEventPublisher;

    @Value("${app.attachments.storage:local}")
    private String attachmentStorage;
    @Value("${app.attachments.dir:./storage/attachments}")
    private String attachmentDir;
    @Value("${app.attachments.s3.bucket:jira-attachments}")
    private String attachmentBucket;

    public List<IssueAttachmentDto> findByIssue(Long issueId) {
        return issueAttachmentRepository.findByIssueIdOrderByCreatedAtDesc(issueId).stream()
                .map(IssueAttachmentDto::from)
                .collect(Collectors.toList());
    }

    @Transactional
    public IssueAttachmentDto upload(Long issueId, MultipartFile file, Long actorUserId, boolean listInAttachmentPanel) {
        if (file == null || file.isEmpty()) {
            throw new RuntimeException("Attachment file is required");
        }
        Issue issue = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(issue.getSpace());
        try {
            String originalFilename = sanitizeFileName(file.getOriginalFilename());
            String storageFilename = UUID.randomUUID() + "_" + originalFilename;
            uploadBinary(storageFilename, file);

            IssueAttachment attachment = new IssueAttachment();
            attachment.setIssue(issue);
            attachment.setUploader(actorUserId != null ? userRepository.findById(actorUserId).orElse(null) : null);
            attachment.setOriginalFilename(originalFilename);
            attachment.setStorageFilename(storageFilename);
            attachment.setContentType(file.getContentType());
            attachment.setSizeBytes(file.getSize());
            attachment.setListInAttachmentPanel(listInAttachmentPanel);
            IssueAttachment saved = issueAttachmentRepository.save(attachment);

            if (listInAttachmentPanel) {
                issueHistoryService.recordEvent(issue, actorUserId, "attachment_uploaded", "attached a file");
                issueHistoryService.recordFieldChange(issue, actorUserId, "Attachment", "None", originalFilename);
            }
            applicationEventPublisher.publishEvent(new AttachmentUploadedEvent(
                    saved.getId(),
                    issue.getId(),
                    issue.getIssueKey(),
                    issue.getSpace().getId(),
                    attachmentStorage,
                    useS3Storage() ? attachmentBucket : "",
                    storageFilename,
                    originalFilename,
                    file.getContentType() != null ? file.getContentType() : "",
                    file.getSize(),
                    listInAttachmentPanel,
                    Instant.now()));
            return IssueAttachmentDto.from(saved);
        } catch (IOException e) {
            throw new RuntimeException("Failed to store attachment", e);
        }
    }

    public ResponseEntity<Resource> download(Long issueId, Long attachmentId) {
        Issue issue = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(issue.getSpace());
        IssueAttachment attachment = getAttachmentInIssue(issueId, attachmentId);
        byte[] bytes = downloadBinary(attachment.getStorageFilename());
        String contentType = attachment.getContentType();
        MediaType mediaType = contentType != null ? MediaType.parseMediaType(contentType) : MediaType.APPLICATION_OCTET_STREAM;
        return ResponseEntity.ok()
                .contentType(mediaType)
                .contentLength(bytes.length)
                .header(HttpHeaders.CONTENT_DISPOSITION, ContentDisposition.attachment().filename(attachment.getOriginalFilename()).build().toString())
                .body(new ByteArrayResource(bytes));
    }

    /**
     * Removes all attachment rows and files for an issue (e.g. cascade issue delete). No history rows.
     */
    @Transactional
    public void purgeAttachmentsForIssue(Long issueId) {
        List<IssueAttachment> list = issueAttachmentRepository.findByIssueIdOrderByCreatedAtDesc(issueId);
        for (IssueAttachment a : list) {
            issueAttachmentRepository.delete(a);
            deleteBinary(a.getStorageFilename());
        }
    }

    @Transactional
    public void delete(Long issueId, Long attachmentId, Long actorUserId) {
        Issue issue = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(issue.getSpace());
        IssueAttachment attachment = getAttachmentInIssue(issueId, attachmentId);
        removeAttachment(issue, attachment, actorUserId, true);
    }

    /**
     * Deletes embedded inline attachments ({@code listInAttachmentPanel=false}) that are no longer
     * referenced in the issue description or any comment HTML.
     */
    @Transactional
    public void reconcileEmbeddedAttachments(Long issueId, Long actorUserId) {
        Issue issue = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(issue.getSpace());

        List<String> htmlFragments = new ArrayList<>();
        if (issue.getDescription() != null) {
            htmlFragments.add(issue.getDescription());
        }
        for (Comment comment : commentRepository.findByIssueIdOrderByCreatedAtAsc(issueId)) {
            if (comment.getContent() != null) {
                htmlFragments.add(comment.getContent());
            }
        }
        Set<Long> referencedIds = AttachmentReferenceExtractor.collectAttachmentIds(
                htmlFragments.toArray(String[]::new));

        List<IssueAttachment> embedded = issueAttachmentRepository.findByIssueIdOrderByCreatedAtDesc(issueId).stream()
                .filter(a -> !a.isListInAttachmentPanel())
                .filter(a -> !referencedIds.contains(a.getId()))
                .toList();

        for (IssueAttachment orphan : embedded) {
            removeAttachment(issue, orphan, actorUserId, false);
        }
    }

    private void removeAttachment(Issue issue, IssueAttachment attachment, Long actorUserId, boolean recordPanelHistory) {
        String filename = attachment.getOriginalFilename();
        String storageFilename = attachment.getStorageFilename();
        String contentType = attachment.getContentType() != null ? attachment.getContentType() : "";
        long sizeBytes = attachment.getSizeBytes();
        boolean listInPanel = attachment.isListInAttachmentPanel();
        long attachmentId = attachment.getId();

        issueAttachmentRepository.delete(attachment);
        deleteBinary(storageFilename);

        issueHistoryService.recordEvent(issue, actorUserId, "attachment_deleted", "removed an attachment");
        if (recordPanelHistory && listInPanel) {
            issueHistoryService.recordFieldChange(issue, actorUserId, "Attachment", filename, "None");
        }

        applicationEventPublisher.publishEvent(new AttachmentDeletedEvent(
                attachmentId,
                issue.getId(),
                issue.getIssueKey(),
                issue.getSpace().getId(),
                attachmentStorage,
                useS3Storage() ? attachmentBucket : "",
                storageFilename,
                filename,
                contentType,
                sizeBytes,
                listInPanel,
                Instant.now()));
    }

    private IssueAttachment getAttachmentInIssue(Long issueId, Long attachmentId) {
        IssueAttachment attachment = issueAttachmentRepository.findById(attachmentId)
                .orElseThrow(() -> new RuntimeException("Attachment not found: " + attachmentId));
        if (!attachment.getIssue().getId().equals(issueId)) {
            throw new RuntimeException("Attachment does not belong to issue");
        }
        return attachment;
    }

    private String sanitizeFileName(String name) {
        if (name == null || name.isBlank()) return "attachment.bin";
        return Path.of(name).getFileName().toString().replaceAll("[\\r\\n]", "_");
    }

    private boolean useS3Storage() {
        return "s3".equalsIgnoreCase(attachmentStorage);
    }

    private void uploadBinary(String storageFilename, MultipartFile file) throws IOException {
        if (!useS3Storage()) {
            Path dir = Path.of(attachmentDir).toAbsolutePath().normalize();
            Files.createDirectories(dir);
            Path destination = dir.resolve(storageFilename);
            Files.copy(file.getInputStream(), destination, StandardCopyOption.REPLACE_EXISTING);
            return;
        }
        ensureBucketExists();
        PutObjectRequest request = PutObjectRequest.builder()
                .bucket(attachmentBucket)
                .key(storageFilename)
                .contentType(file.getContentType())
                .build();
        s3Client.putObject(request, RequestBody.fromInputStream(file.getInputStream(), file.getSize()));
    }

    private byte[] downloadBinary(String storageFilename) {
        if (!useS3Storage()) {
            try {
                Path path = Path.of(attachmentDir).toAbsolutePath().normalize().resolve(storageFilename);
                if (!Files.exists(path)) {
                    throw new RuntimeException("Attachment file missing on disk");
                }
                return Files.readAllBytes(path);
            } catch (IOException e) {
                throw new RuntimeException("Failed to download attachment", e);
            }
        }
        try {
            ResponseBytes<?> bytes = s3Client.getObjectAsBytes(GetObjectRequest.builder()
                    .bucket(attachmentBucket)
                    .key(storageFilename)
                    .build());
            return bytes.asByteArray();
        } catch (S3Exception e) {
            throw new RuntimeException("Failed to download attachment from S3", e);
        }
    }

    private void deleteBinary(String storageFilename) {
        if (!useS3Storage()) {
            try {
                Path path = Path.of(attachmentDir).toAbsolutePath().normalize().resolve(storageFilename);
                Files.deleteIfExists(path);
            } catch (IOException ignored) {
            }
            return;
        }
        try {
            s3Client.deleteObject(DeleteObjectRequest.builder()
                    .bucket(attachmentBucket)
                    .key(storageFilename)
                    .build());
        } catch (S3Exception ignored) {
        }
    }

    private void ensureBucketExists() {
        try {
            s3Client.headBucket(HeadBucketRequest.builder().bucket(attachmentBucket).build());
        } catch (NoSuchBucketException e) {
            s3Client.createBucket(CreateBucketRequest.builder().bucket(attachmentBucket).build());
        } catch (S3Exception e) {
            if (e.statusCode() == 404) {
                s3Client.createBucket(CreateBucketRequest.builder().bucket(attachmentBucket).build());
            } else {
                throw e;
            }
        }
    }
}
