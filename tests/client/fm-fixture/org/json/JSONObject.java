package org.json;

import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;

/** Only the readiness serializer is exercised; plan parsing stays an Android gate. */
public class JSONObject {
    private final Map<String, Object> values = new LinkedHashMap<>();
    public JSONObject() {}
    public JSONObject(String text) { throw new UnsupportedOperationException(); }
    public JSONObject put(String key, Object value) { values.put(key, value); return this; }
    public int getInt(String key) { throw new UnsupportedOperationException(); }
    public String getString(String key) { throw new UnsupportedOperationException(); }
    public JSONObject getJSONObject(String key) { throw new UnsupportedOperationException(); }
    public Iterator<String> keys() { return values.keySet().iterator(); }
    @Override public String toString() {
        StringBuilder result = new StringBuilder("{");
        for (Map.Entry<String, Object> item : values.entrySet()) {
            if (result.length() > 1) result.append(",");
            result.append("\"").append(item.getKey()).append("\":");
            if (item.getValue() instanceof Number) result.append(item.getValue());
            else result.append("\"").append(item.getValue()).append("\"");
        }
        return result.append("}").toString();
    }
}
