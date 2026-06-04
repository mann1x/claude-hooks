#include <iostream>
#include <string>
#include <vector>
int main(){
    std::string s;
    std::getline(std::cin, s);
    while(!s.empty() && (s.back()=='\r'||s.back()=='\n')) s.pop_back();
    std::vector<char> st;
    bool ok = true;
    for(char c : s){
        if(c=='('||c=='['||c=='{') st.push_back(c);
        else if(c==')'||c==']'||c=='}'){
            char m = c==')'?'(':(c==']'?'[':'{');
            if(st.empty() || st.back()!=m){ ok=false; break; }
            st.pop_back();
        }
    }
    std::cout << ((ok && st.empty()) ? "YES" : "NO") << "\n";
    return 0;
}
