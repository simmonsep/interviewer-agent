import java.io.*;
import java.util.*;
public class Main {
    public static void main(String[] args) throws Exception {
        Scanner in = new Scanner(System.in);
        int n = in.nextInt();
        Map<String,String> records = new HashMap<>();
        for (int i=0; i<n; i++) {
            String key=in.next(), payload=in.next();
            if (!records.containsKey(key)) {
                records.put(key,payload);
                System.out.println("NEW");
            } else {
                System.out.println(records.get(key).equals(payload) ? "REPLAY" : "CONFLICT");
            }
        }
    }
}
