package com.jiraagentic.app.util;

import java.util.HashSet;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Parses {@code data-attachment-id} values from issue/comment HTML.
 */
public final class AttachmentReferenceExtractor {

    private static final Pattern ATTACHMENT_ID =
            Pattern.compile("data-attachment-id\\s*=\\s*[\"']?(\\d+)[\"']?", Pattern.CASE_INSENSITIVE);

    private AttachmentReferenceExtractor() {}

    public static Set<Long> collectAttachmentIds(String... htmlFragments) {
        Set<Long> ids = new HashSet<>();
        if (htmlFragments == null) {
            return ids;
        }
        for (String html : htmlFragments) {
            if (html == null || html.isBlank()) {
                continue;
            }
            Matcher matcher = ATTACHMENT_ID.matcher(html);
            while (matcher.find()) {
                ids.add(Long.parseLong(matcher.group(1)));
            }
        }
        return ids;
    }
}
