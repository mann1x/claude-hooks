#include <iostream>
#include <string>
#include <algorithm>
int main() {
    std::string s;
    std::getline(std::cin, s);
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n'))
        s.pop_back();
    std::reverse(s.begin(), s.end());
    std::cout << s << std::endl;
    return 0;
}
