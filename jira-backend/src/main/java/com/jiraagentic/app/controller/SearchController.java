package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.SearchResultDto;
import com.jiraagentic.app.security.AuthSupport;
import com.jiraagentic.app.service.SearchService;
import lombok.RequiredArgsConstructor;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/search")
@RequiredArgsConstructor
public class SearchController {

    private final SearchService searchService;

    @GetMapping
    public List<SearchResultDto> search(
            @RequestParam String q,
            @RequestParam(required = false) Integer limit,
            Authentication authentication) {
        return searchService.search(AuthSupport.extractUid(authentication), q, limit);
    }
}
