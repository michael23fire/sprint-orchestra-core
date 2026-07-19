package com.jiraagentic.app.service;

import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.Label;
import com.jiraagentic.app.entity.Space;
import com.jiraagentic.app.repository.IssueRepository;
import com.jiraagentic.app.repository.LabelRepository;
import com.jiraagentic.app.repository.SpaceRepository;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class LabelServiceTest {

    @Mock LabelRepository labelRepository;
    @Mock IssueRepository issueRepository;
    @Mock SpaceRepository spaceRepository;
    @Mock IssueHistoryService issueHistoryService;
    @Mock ActiveSpaceGuard activeSpaceGuard;

    @Test
    void createsLowercaseLabelWithCanonicalDisplayName() {
        Space space = new Space();
        space.setId(1L);
        when(spaceRepository.findActiveByIdForUpdate(1L)).thenReturn(Optional.of(space));
        when(labelRepository.findBySpace_IdAndNormalizedName(1L, "testing")).thenReturn(Optional.empty());
        when(labelRepository.save(any(Label.class))).thenAnswer(invocation -> {
            Label label = invocation.getArgument(0);
            label.setId(10L);
            return label;
        });

        LabelService service = service();
        Label created = service.resolveLabels(1L, List.of("  testing  ")).iterator().next();

        assertThat(created.getName()).isEqualTo("Testing");
        assertThat(created.getNormalizedName()).isEqualTo("testing");
        assertThat(created.getSpace()).isSameAs(space);
    }

    @Test
    void reusesExistingLabelIgnoringCaseAndPreservesItsSpelling() {
        Space space = new Space();
        space.setId(1L);
        Label existing = new Label();
        existing.setId(7L);
        existing.setSpace(space);
        existing.setName("DevOps");
        existing.setNormalizedName("devops");
        when(spaceRepository.findActiveByIdForUpdate(1L)).thenReturn(Optional.of(space));
        when(labelRepository.findBySpace_IdAndNormalizedName(1L, "devops")).thenReturn(Optional.of(existing));

        Label resolved = service().resolveLabels(1L, List.of("DEVOPS")).iterator().next();

        assertThat(resolved).isSameAs(existing);
        assertThat(resolved.getName()).isEqualTo("DevOps");
        verify(labelRepository, never()).save(any(Label.class));
    }

    @Test
    void deletingLabelRemovesItFromEveryIssueAndRecordsHistory() {
        Space space = new Space();
        space.setId(1L);
        Label label = new Label();
        label.setId(7L);
        label.setSpace(space);
        label.setName("Testing");
        Issue issue = new Issue();
        issue.setId(20L);
        issue.setLabels(new LinkedHashSet<>(List.of(label)));

        when(activeSpaceGuard.requireActive(1L)).thenReturn(space);
        when(labelRepository.findById(7L)).thenReturn(Optional.of(label));
        when(issueRepository.findDistinctByLabels_Id(7L)).thenReturn(List.of(issue));

        service().delete(1L, 7L, 99L);

        assertThat(issue.getLabels()).isEmpty();
        assertThat(label.getDeletedAt()).isNotNull();
        verify(issueHistoryService).recordFieldChange(issue, 99L, "labels", "Testing", "");
        verify(issueRepository).saveAll(List.of(issue));
        verify(labelRepository).save(label);
    }

    private LabelService service() {
        return new LabelService(
                labelRepository,
                issueRepository,
                spaceRepository,
                issueHistoryService,
                activeSpaceGuard);
    }
}
