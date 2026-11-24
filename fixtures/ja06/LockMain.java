import java.io.*;
import java.util.*;
public class Main {
    public static void main(String[] args) throws Exception {
        Scanner in = new Scanner(System.in);
        int n=in.nextInt();
        String owner=null;
        long expires=0;
        for (int i=0; i<n; i++) {
            String op=in.next();
            long now=in.nextLong();
            if (owner!=null && now>=expires) owner=null;
            if (op.equals("SET")) {
                String token=in.next(); long ttl=in.nextLong();
                if (owner==null) {
                    owner=token; expires=now+ttl;
                    System.out.println("OK");
                } else System.out.println("BUSY");
            } else if (op.equals("RELEASE")) {
                String token=in.next();
                if (owner!=null && owner.equals(token)) {
                    owner=null; System.out.println("RELEASED");
                } else System.out.println("NOT_OWNER");
            } else System.out.println(owner==null ? "EMPTY" : owner);
        }
    }
}
