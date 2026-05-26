package com.jiraagentic.app.dto;

import com.jiraagentic.app.entity.IssueAttachment;
import lombok.Data;

import java.time.Instant;

@Data
public class IssueAttachmentDto {
    private Long id;
    private Long issueId;
    private Long uploaderId;
    private String uploaderName;
    private String originalFilename;
    private String contentType;
    private Long sizeBytes;
    private Instant createdAt;
    private Boolean listInAttachmentPanel;

    public static IssueAttachmentDto from(IssueAttachment a) {
        IssueAttachmentDto dto = new IssueAttachmentDto();
        dto.setId(a.getId());
        dto.setIssueId(a.getIssue().getId());
        if (a.getUploader() != null) {
            dto.setUploaderId(a.getUploader().getId());
            dto.setUploaderName(a.getUploader().getName());
        }
        dto.setOriginalFilename(a.getOriginalFilename());
        dto.setContentType(a.getContentType());
        dto.setSizeBytes(a.getSizeBytes());
        dto.setCreatedAt(a.getCreatedAt());
        dto.setListInAttachmentPanel(a.isListInAttachmentPanel());
        return dto;
    }
}
