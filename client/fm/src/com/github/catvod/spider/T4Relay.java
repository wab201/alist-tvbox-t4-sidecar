package com.github.catvod.spider;

import java.util.Map;

/** Explicit siteKey dispatch avoids interference from other JAR-based sites. */
public final class T4Relay extends com.github.catvod.crawler.Spider {
    @Override public Object[] proxy(Map<String, String> params) throws Exception {
        return Proxy.proxy(params);
    }
}
